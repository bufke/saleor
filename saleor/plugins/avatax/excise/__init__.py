import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin

import opentracing
import opentracing.tags
import requests
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from requests.auth import HTTPBasicAuth

from ....checkout import base_calculations
from ....core.taxes import TaxError
from .. import TransactionType

if TYPE_CHECKING:
    # flake8: noqa
    from ....checkout.models import Checkout, CheckoutLine
    from ....order.models import Order
    from ....product.models import Product, ProductType, ProductVariant

logger = logging.getLogger(__name__)


@dataclass
class AvataxConfiguration:
    username_or_account: str
    password_or_license: str
    use_sandbox: bool = True
    company_name: str = "DEFAULT"
    autocommit: bool = False


def get_api_url(use_sandbox=True) -> str:
    """Based on settings return sanbox or production url."""
    if use_sandbox:
        return "https://excisesbx.avalara.com/api/v1/"
    return "https://excise.avalara.net/api/v1/"


def api_post_request(
    url: str, data: Dict[str, Any], config: AvataxConfiguration
) -> Dict[str, Any]:
    response = None
    try:
        auth = HTTPBasicAuth(config.username_or_account, config.password_or_license)
        response = requests.post(url, auth=auth, data=json.dumps(data), timeout=TIMEOUT)
        logger.debug("Hit to Avatax to calculate taxes %s", url)
        json_response = response.json()
        if "error" in response:  # type: ignore
            logger.exception("Avatax response contains errors %s", json_response)
            return json_response
    except requests.exceptions.RequestException:
        logger.exception("Fetching taxes failed %s", url)
        return {}
    except json.JSONDecodeError:
        content = response.content if response else "Unable to find the response"
        logger.exception(
            "Unable to decode the response from Avatax. Response: %s", content
        )
        return {}
    return json_response  # type: ignore


def api_get_request(
    url: str,
    username_or_account: str,
    password_or_license: str,
):
    response = None
    try:
        auth = HTTPBasicAuth(username_or_account, password_or_license)
        response = requests.get(url, auth=auth, timeout=TIMEOUT)
        json_response = response.json()
        logger.debug("[GET] Hit to %s", url)
        if "error" in json_response:  # type: ignore
            logger.error("Avatax response contains errors %s", json_response)
        return json_response
    except requests.exceptions.RequestException:
        logger.exception("Failed to fetch data from %s", url)
        return {}
    except json.JSONDecodeError:
        content = response.content if response else "Unable to find the response"
        logger.exception(
            "Unable to decode the response from Avatax. Response: %s", content
        )
        return {}


def generate_request_data(
    transaction_type: str,
    lines: List[Dict[str, Any]],
    transaction_token: str,
    address: Dict[str, str],
    customer_email: str,
    config: AvataxConfiguration,
    currency: str,
):
    company_address = Site.objects.get_current().settings.company_address
    if company_address:
        company_address = company_address.as_data()
    else:
        logging.warning(
            "To correct calculate taxes by Avatax, company address should be provided "
            "in dashboard.settings."
        )
        company_address = {}

    print(lines)
    # fun = [
    #     {
    #         "quantity": 1,
    #         "amount": "7.00",
    #         "taxCode": "O9999999",
    #         "taxIncluded": True,
    #         "itemCode": "80884671",
    #         "description": "Apple Juice",
    #     },
    #     {
    #         "quantity": 1,
    #         "amount": "29.830",
    #         "taxCode": "FR020100",
    #         "taxIncluded": True,
    #         "itemCode": "Shipping",
    #         "description": None,
    #     },
    # ]

    # transaction_lines = [{"InvoiceLine": } for line in lines]

    data = {"TransactionLines": [lines]}

    data = {
        "companyCode": config.company_name,
        "type": transaction_type,
        "lines": lines,
        "code": transaction_token,
        "date": str(date.today()),
        # https://developer.avalara.com/avatax/dev-guide/transactions/simple-transaction/
        "customerCode": 0,
        "addresses": {
            "shipFrom": {
                "line1": company_address.get("street_address_1"),
                "line2": company_address.get("street_address_2"),
                "city": company_address.get("city"),
                "region": company_address.get("country_area"),
                "country": company_address.get("country"),
                "postalCode": company_address.get("postal_code"),
            },
            "shipTo": {
                "line1": address.get("street_address_1"),
                "line2": address.get("street_address_2"),
                "city": address.get("city"),
                "region": address.get("country_area"),
                "country": address.get("country"),
                "postalCode": address.get("postal_code"),
            },
        },
        "commit": config.autocommit,
        "currencyCode": currency,
        "email": customer_email,
    }
    return {"createTransactionModel": data}


@dataclass
class TransactionLine:
    InvoiceLine: int
    ProductCode: str
    UnitPrice: Decimal
    BilledUnits: Decimal
    AlternateUnitPrice: Optional[Decimal]
    TaxIncluded: bool
    DestinationCountryCode: str
    """ ISO 3166-1 alpha-3 code """
    DestinationJurisdiction: str
    DestinationAddress1: Optional[str]
    DestinationAddress2: Optional[str]


def get_checkout_lines_data(
    checkout: "Checkout", discounts=None
) -> List[Dict[str, Union[str, int, bool, None]]]:
    data: List[Dict[str, Union[str, int, bool, None]]] = []
    lines = checkout.lines.prefetch_related(
        "variant__product__category",
        "variant__product__collections",
        "variant__product__product_type",
    ).filter(variant__product__charge_taxes=True)
    tax_included = Site.objects.get_current().settings.include_taxes_in_prices
    channel = checkout.channel

    for line in lines:
        channel_listing = line.variant.channel_listings.get(channel=channel)
        stock = line.variant.stocks.for_country(
            checkout.shipping_address.country
        ).first()
        data.append(
            TransactionLine(
                InvoiceLine=line.id,
                ProductCode=line.variant.sku,
                UnitPrice=channel_listing.price.amount,
                BilledUnits=line.quantity,
                AlternateUnitPrice=channel_listing.cost_price.amount
                if channel_listing.cost_price
                else None,
                TaxIncluded=tax_included,
                DestinationCountryCode=checkout.shipping_address.country.alpha3,
                DestinationJurisdiction=checkout.shipping_address.country_area,
                DestinationAddress1=checkout.shipping_address.street_address_1,
                DestinationAddress2=checkout.shipping_address.street_address_2,
            )
        )
        # name = line.variant.product.name
        # product = line.variant.product
        # collections = product.collections.all()
        # channel_listing = line.variant.channel_listings.get(channel=channel)
        # product_type = line.variant.product.product_type
        # tax_code = retrieve_tax_code_from_meta(product, default=None)
        # tax_code = tax_code or retrieve_tax_code_from_meta(product_type)
        # append_line_to_data(
        #     data=data,
        #     quantity=line.quantity,
        #     amount=base_calculations.base_checkout_line_total(
        #         line,
        #         line.variant,
        #         product,
        #         collections,
        #         channel,
        #         channel_listing,
        #         discounts,
        #     ).gross.amount,
        #     tax_code=tax_code,
        #     item_code=line.variant.sku,
        #     name=name,
        # )

    # append_shipping_to_data(data, checkout.shipping_method, checkout.channel_id)
    return data


def generate_request_data_from_checkout(
    checkout: "Checkout",
    config: AvataxConfiguration,
    transaction_token=None,
    transaction_type=TransactionType.ORDER,
    discounts=None,
):

    address = checkout.shipping_address or checkout.billing_address
    lines = get_checkout_lines_data(checkout, discounts)

    currency = checkout.currency
    data = generate_request_data(
        transaction_type=transaction_type,
        lines=lines,
        transaction_token=transaction_token or str(checkout.token),
        address=address.as_data() if address else {},
        customer_email=checkout.email,
        config=config,
        currency=currency,
    )
    return data


def get_checkout_tax_data(
    checkout: "Checkout", discounts, config: AvataxConfiguration
) -> Dict[str, Any]:
    data = generate_request_data_from_checkout(checkout, config, discounts=discounts)
