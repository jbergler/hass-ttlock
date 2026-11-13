"""Config flow for TTLock."""

import logging
import secrets
from typing import Any

import probatio as vol

from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BLUETOOTH_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_SLOW_POLL_INTERVAL,
    CONF_WEBHOOK_URL,
    DEFAULT_BLUETOOTH_ENABLED,
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
        """Resolve the webhook id then let the user copy the URL before creating.

        Entries sharing one client_id's developer app also share one webhook
        registration (see webhook.py), so if a sibling entry already exists
        under this flow's client_id, adopt its webhook_id up front - setup's
        _resolve_group_webhook_id would reconcile it anyway, and showing the
        canonical URL here means the user doesn't paste a soon-to-be-replaced
        guess into the TTLock console.
        """
        data[CONF_REGION] = self._region
        auth_impl = self.flow_impl.domain
        data[CONF_WEBHOOK_ID] = (
            _sibling_webhook_id(self.hass, auth_impl) or secrets.token_hex()
        )
        self._oauth_data = data
        return await self.async_step_webhook()

    async def async_step_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the webhook URL so the user can register it with TTLock.

        A second, persistent copy also lives in the options flow and in the
        webhook_setup repair that fires if traffic never arrives, so the user
        can come back to this URL after setup if they missed this step.
        """
        if user_input is not None:
            return await super().async_oauth_create_entry(self._oauth_data)
        return self.async_show_form(
            step_id="webhook",
            description_placeholders={
                "webhook_url": _safe_webhook_url(
                    self.hass, self._oauth_data[CONF_WEBHOOK_ID]
                ),
                "console_url": "https://open.ttlock.com/manager",
                "docs_url": "https://github.com/jbergler/hass-ttlock?tab=readme-ov-file#creating-an-oauth-app",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for polling cadence and Bluetooth."""
        return TTLockOptionsFlow()


class TTLockOptionsFlow(OptionsFlow):
    """Let users tune how the integration reaches their locks.

    Three knobs, all global to the account entry: the fast poll interval
    that re-verifies lock state, the slow interval that governs how often
    detail/passage/gateway data is re-fetched (see coordinator.py), and
    whether locks may be read over Bluetooth at all. Defaults are gentle
    because webhooks carry real-time changes; users whose webhooks are
    unreliable can dial the fast interval back down. Changing any of them
    reloads the entry so new coordinators pick the values up.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the polling and Bluetooth options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        webhook_id = self.config_entry.data.get(CONF_WEBHOOK_ID)
        webhook_url = self.config_entry.data.get(CONF_WEBHOOK_URL) or (
            _safe_webhook_url(self.hass, webhook_id) if webhook_id else ""
        )
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
                    vol.Required(
                        CONF_BLUETOOTH_ENABLED,
                        default=options.get(
                            CONF_BLUETOOTH_ENABLED, DEFAULT_BLUETOOTH_ENABLED
                        ),
                    ): BooleanSelector(),
                }
            ),
            description_placeholders={"webhook_url": webhook_url},
        )


def _sibling_webhook_id(hass, auth_implementation: str) -> str | None:
    """Return the webhook_id another entry under this credential already uses.

    Keyed off the credential's auth_implementation domain, which is the same
    for every entry authorised against one TTLock developer application - no
    need to rebuild the OAuth implementation to compare client_ids.
    """
    return next(
        (
            webhook_id
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.data.get("auth_implementation") == auth_implementation
            and (webhook_id := entry.data.get(CONF_WEBHOOK_ID))
        ),
        None,
    )


def _safe_webhook_url(hass, webhook_id: str) -> str:
    """Best-effort webhook URL - empty if HA has no base URL configured yet."""
    from homeassistant.helpers.network import (  # noqa: PLC0415
        NoURLAvailableError,
    )

    try:
        return webhook.async_generate_url(hass, webhook_id)
    except NoURLAvailableError:
        return ""
