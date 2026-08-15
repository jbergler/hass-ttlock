"""Config flow for TTLock."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_REGION, DEFAULT_REGION, DOMAIN, REGIONS


class TTLockAuthFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow to handle TTLock OAuth2 authentication."""

    DOMAIN = DOMAIN
    _region: str = DEFAULT_REGION

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return logging.getLogger(__name__)

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a region and authenticate against that region's cloud."""
        # Flow has been triggered by external data
        errors = {}
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            # TTLock runs separate per-region clouds; point the OAuth token
            # request at the chosen region before logging in. The runtime
            # implementation is rebuilt from entry.data[CONF_REGION] on setup
            # (see application_credentials.async_get_auth_implementation), so
            # this flow-time mutation only needs to hold for login() here.
            self.flow_impl.token_url = REGIONS[self._region]["token_url"]  # ty: ignore[unresolved-attribute] - flow_impl is a TTLockAuthImplementation
            session = await self.flow_impl.login(  # ty: ignore[unresolved-attribute] - flow_impl is a TTLockAuthImplementation
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
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
                    vol.Required(CONF_REGION, default=self._region): SelectSelector(
                        SelectSelectorConfig(
                            options=list(REGIONS),
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="region",
                        )
                    ),
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Persist the selected region alongside the OAuth token."""
        data[CONF_REGION] = self._region
        return await super().async_oauth_create_entry(data)
