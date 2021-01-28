import logging
from decimal import Decimal
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

import opentracing
import opentracing.tags
from django.core.exceptions import ValidationError
from prices import Money

from ....core.taxes import TaxedMoney, TaxError
from ....discount import DiscountInfo
from ...error_codes import PluginErrorCode
from .. import _validate_checkout
from ..plugin import AvataxPlugin
from . import api_get_request, api_post_request, get_api_url, get_checkout_tax_data

logger = logging.getLogger(__name__)


class AvataxExcisePlugin(AvataxPlugin):
    PLUGIN_NAME = "Avalara Excise"
    PLUGIN_ID = "mirumee.taxes.avalara_excise"

    @classmethod
    def validate_authentication(cls, plugin_configuration: "PluginConfiguration"):
        conf = {
            data["name"]: data["value"] for data in plugin_configuration.configuration
        }
        url = urljoin(get_api_url(conf["Use sandbox"]), "utilities/ping")
        response = api_get_request(
            url,
            username_or_account=conf["Username or account"],
            password_or_license=conf["Password or license"],
        )

        if not response.get("authenticated"):
            raise ValidationError(
                "Authentication failed. Please check provided data.",
                code=PluginErrorCode.PLUGIN_MISCONFIGURED.value,
            )

    def calculate_checkout_line_total(
        self,
        checkout: "Checkout",
        checkout_line: "CheckoutLine",
        variant: "ProductVariant",
        product: "Product",
        collections: Iterable["Collection"],
        address: Optional["Address"],
        channel: "Channel",
        channel_listing: "ProductVariantChannelListing",
        discounts: Iterable[DiscountInfo],
        previous_value: TaxedMoney,
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        base_total = previous_value
        if not checkout_line.variant.product.charge_taxes:
            return base_total

        if not _validate_checkout(checkout, [checkout_line]):
            return base_total

        taxes_data = get_checkout_tax_data(checkout, discounts, self.config)
        if not taxes_data or "error" in taxes_data:
            return base_total

        currency = taxes_data.get("currencyCode")
        for line in taxes_data.get("lines", []):
            if line.get("itemCode") == variant.sku:
                tax = Decimal(line.get("tax", 0.0))
                line_net = Decimal(line["lineAmount"])
                line_gross = Money(amount=line_net + tax, currency=currency)
                line_net = Money(amount=line_net, currency=currency)
                return TaxedMoney(net=line_net, gross=line_gross)

        return base_total
