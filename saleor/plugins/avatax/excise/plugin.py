import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Optional, Union
from urllib.parse import urljoin

import opentracing
import opentracing.tags
from django.core.exceptions import ValidationError
from prices import Money, TaxedMoney, TaxedMoneyRange

from ....core.taxes import TaxError
from ....discount import DiscountInfo
from ...base_plugin import BasePlugin, ConfigurationTypeField
from ...error_codes import PluginErrorCode
from .. import _validate_checkout
from . import api_get_request, api_post_request, get_api_url, get_checkout_tax_data

logger = logging.getLogger(__name__)


@dataclass
class AvataxConfiguration:
    username: str
    password: str
    use_sandbox: bool = True
    company_id: str = None


class AvataxExcisePlugin(BasePlugin):
    PLUGIN_NAME = "Avalara Excise"
    PLUGIN_ID = "mirumee.taxes.avalara_excise"

    DEFAULT_CONFIGURATION = [
        {"name": "Username", "value": None},
        {"name": "Password", "value": None},
        {"name": "Use sandbox", "value": True},
        {"name": "Company ID", "value": None},
    ]
    CONFIG_STRUCTURE = {
        "Username": {
            "type": ConfigurationTypeField.STRING,
            "help_text": "Provide user details",
            "label": "Username",
        },
        "Password": {
            "type": ConfigurationTypeField.PASSWORD,
            "help_text": "Provide password details",
            "label": "Password",
        },
        "Use sandbox": {
            "type": ConfigurationTypeField.BOOLEAN,
            "help_text": "Determines if Saleor should use Avatax Excise sandbox API.",
            "label": "Use sandbox",
        },
        "Company ID": {
            "type": ConfigurationTypeField.STRING,
            "help_text": "Avalara company ID.",
            "label": "Company ID",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Convert to dict to easier take config elements
        configuration = {item["name"]: item["value"] for item in self.configuration}
        self.config = AvataxConfiguration(
            username=configuration["Username"],
            password=configuration["Password"],
            use_sandbox=configuration["Use sandbox"],
            company_id=configuration["Company ID"],
        )

    def _skip_plugin(
        self, previous_value: Union[TaxedMoney, TaxedMoneyRange, Decimal]
    ) -> bool:
        if not (self.config.username and self.config.password):
            return True

        if not self.active:
            return True

        # The previous plugin already calculated taxes so we can skip our logic
        if isinstance(previous_value, TaxedMoneyRange):
            start = previous_value.start
            stop = previous_value.stop

            return start.net != start.gross and stop.net != stop.gross

        if isinstance(previous_value, TaxedMoney):
            return previous_value.net != previous_value.gross
        return False

    @classmethod
    def validate_authentication(cls, plugin_configuration: "PluginConfiguration"):
        conf = {
            data["name"]: data["value"] for data in plugin_configuration.configuration
        }
        url = urljoin(get_api_url(conf["Use sandbox"]), "utilities/ping")
        response = api_get_request(
            url,
            username_or_account=conf["Username"],
            password_or_license=conf["Password"],
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
        # if self._skip_plugin(previous_value):
        #     print("skip plugin")
        #     return previous_value

        base_total = previous_value
        if not checkout_line.variant.product.charge_taxes:
            print("dont charge taxes")
            return base_total

        if not _validate_checkout(checkout, [checkout_line]):
            print("checkout not valid")
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
