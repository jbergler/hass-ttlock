"""Config flow for TTLock."""
import logging
from typing import Any
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResult

from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN


class TTLockAuthFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow to handle TTLock OAuth2 authentication."""

    DOMAIN = DOMAIN

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return logging.getLogger(__name__)

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create an entry for auth."""
        # Flow has been triggered by external data
        errors = {}
        if user_input is not None:
            session = await self.flow_impl.login(
                user_input["username"], user_input["password"]
            )
            if "errmsg" in session:
                errors["base"] = session["errmsg"]
            else:
                self.external_data = session
                return await self.async_step_creation()

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )
