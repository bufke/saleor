import json
import logging
import dataclasses
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
    data_dict = dataclasses.asdict(data)
    print('data dictttt', data_dict)
    try:
        auth = HTTPBasicAuth(config.username_or_account, config.password_or_license)
        response = requests.post(
            url, auth=auth, data=json.dumps(data_dict), timeout=TIMEOUT)
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


@dataclass
class RequestData:
    EffectiveDate: date
    InvoiceDate: date
    InvoiceNumber: str
    TitleTransferCode: str
    TransactionType: str
    TransactionLines: List[Dict[str, Union[str, int, bool, None]]]


def generate_request_data(
    lines: List[Dict[str, Any]],
    checkout: "Checkout",
    config: AvataxConfiguration,
):
    checkout_id = str(checkout.token)
    data: Dict = {}
    date = checkout.last_change.strftime('%m/%d/%y')
    data = RequestData(
        EffectiveDate=date,
        InvoiceDate=date,
        InvoiceNumber=checkout_id,
        TitleTransferCode="DEST",
        TransactionType="RETAIL",
        TransactionLines=lines
    )

    # return {"createTransactionModel": data}
    return data


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
    DestinationCounty: Optional[str]
    DestinationCity: str
    DestinationPostalCode: str
    SaleCountryCode: str
    SaleAddress1: Optional[str]
    SaleAddress2: Optional[str]
    SaleJurisdiction: str
    SaleCounty: Optional[str]
    SaleCity: str
    SalePostalCode: str

    """ WIP """
    Origin: Optional[str]
    OriginCountryCode: str
    OriginJurisdiction: str  # state or region
    OriginCounty: str
    OriginCity: str
    OriginPostalCode: str
    OriginAddress1: str
    OriginAddress2: Optional[str]


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
    shipping_address = checkout.shipping_address

    for line in lines:
        channel_listing = line.variant.channel_listings.get(channel=channel)
        stock = line.variant.stocks.for_country(
            checkout.shipping_address.country
        ).first()
        warehouse = stock.warehouse
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
                DestinationCountryCode=shipping_address.country.alpha3,
                DestinationJurisdiction=shipping_address.country_area,
                DestinationAddress1=shipping_address.street_address_1,
                DestinationAddress2=shipping_address.street_address_2,
                DestinationCity=shipping_address.city,
                DestinationCounty=shipping_address.city_area,
                DestinationPostalCode=shipping_address.postal_code,
                SaleCountryCode=shipping_address.country.alpha3,
                SaleJurisdiction=shipping_address.country_area,
                SaleAddress1=shipping_address.street_address_1,
                SaleAddress2=shipping_address.street_address_2,
                SaleCity=shipping_address.city,
                SaleCounty=shipping_address.city_area,
                SalePostalCode=shipping_address.postal_code,
                Origin=warehouse.id,  # check with avalara?
                OriginCountryCode=warehouse.address.country.alpha3,
                OriginJurisdiction=warehouse.address.country_area,
                OriginAddress1=warehouse.address.street_address_1,
                OriginAddress2=warehouse.address.street_address_2,
                OriginCity=warehouse.address.city,
                OriginCounty=warehouse.address.city_area,
                OriginPostalCode=warehouse.address.postal_code,
            )
        )

    # append_shipping_to_data(data, checkout.shipping_method, checkout.channel_id)
    return data


def generate_request_data_from_checkout(
    checkout: "Checkout",
    config: AvataxConfiguration,
    transaction_token=None,
    transaction_type=TransactionType.ORDER,
    discounts=None,
):
    lines = get_checkout_lines_data(checkout, discounts)
    data = generate_request_data(
        checkout=checkout,
        lines=lines,
        config=config,
    )

    return data


def get_checkout_tax_data(
    checkout: "Checkout", discounts, config: AvataxConfiguration
) -> Dict[str, Any]:
    data = generate_request_data_from_checkout(checkout, config, discounts=discounts)
    url = get_api_url()
    print("data tyyype", data)
    tax_response = api_post_request(url, data, config)
    print("taxxx", tax_response)
