"""Config flow for TTLock."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_SLOW_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_REGION,
    DEFAULT_SLOW_POLL_INTERVAL_HOURS,
    DOMAIN,
    REGIONS,
)


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for tuning polling cadence."""
        return TTLockOptionsFlow()


class TTLockOptionsFlow(OptionsFlow):
    """Let users tune how often the integration polls the TTLock cloud.

    Two knobs, both global to the account entry: the fast poll interval that
    re-verifies lock state, and the slow interval that governs how often
    detail/passage/gateway data is re-fetched (see coordinator.py). Defaults
    are gentle because webhooks carry real-time changes; users whose webhooks
    are unreliable can dial the fast interval back down. Changing either
    reloads the entry so new coordinators pick the values up.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the polling-cadence options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_MINUTES
                        ),
                    ): vol.All(
                        NumberSelector(
                            NumberSelectorConfig(
                                min=5,
                                max=1440,
                                step=1,
                                unit_of_measurement="minutes",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        # NumberSelector yields floats; keep stored options as
                        # ints so the reload guard's snapshot compares cleanly.
                        vol.Coerce(int),
                    ),
                    vol.Required(
                        CONF_SLOW_POLL_INTERVAL,
                        default=options.get(
                            CONF_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL_HOURS
                        ),
                    ): vol.All(
                        NumberSelector(
                            NumberSelectorConfig(
                                min=1,
                                max=168,
                                step=1,
                                unit_of_measurement="hours",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Coerce(int),
                    ),
                }
            ),
        )
