from urllib.parse import urljoin

from django.core.exceptions import ValidationError

from ...error_codes import PluginErrorCode
from .. import api_get_request
from ..plugin import AvataxPlugin
from . import get_api_url


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
