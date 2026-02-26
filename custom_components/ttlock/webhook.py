"""The TTLock integration."""

from __future__ import annotations

import json
import logging
import secrets

from aiohttp.web import Request

from homeassistant.components import persistent_notification, webhook
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_WEBHOOK_ID,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError

from .const import CONF_WEBHOOK_STATUS, CONF_WEBHOOK_URL, DOMAIN, SIGNAL_NEW_DATA
from .models import WebhookEvent

_LOGGER = logging.getLogger(__name__)


class WebhookHandler:
    """Responsible for setting up/processing webhook data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Init the thing."""
        self.hass = hass
        self.entry = entry

    async def setup(self) -> None:
        """Actually register the webhook."""
        if self.hass.state == CoreState.running:
            await self.register_webhook()
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self.register_webhook
            )

    async def try_generate_cloudhook(self) -> str | None:
        """Create a cloudhook if possible."""
        # separate function so we can mock
        from homeassistant.components import cloud

        if cloud.async_active_subscription(self.hass):
            try:
                return await cloud.async_create_cloudhook(
                    self.hass, self.entry.data[CONF_WEBHOOK_ID]
                )
            except cloud.CloudNotConnected:
                return None
        return None

    async def get_url(self) -> str:
        """Get the webhook url depending on the setup."""
        if CONF_WEBHOOK_URL in self.entry.data:
            return self.entry.data[CONF_WEBHOOK_URL]
        if cloudhook := await self.try_generate_cloudhook():
            return cloudhook
        else:
            return webhook.async_generate_url(
                self.hass, self.entry.data[CONF_WEBHOOK_ID]
            )

    async def register_webhook(self, event: Event | None = None) -> None:
        """Set up a webhook to receive pushed data."""
        if CONF_WEBHOOK_ID not in self.entry.data:
            _LOGGER.info("Webhook not found in config entry, creating new one")
            data = {**self.entry.data, CONF_WEBHOOK_ID: secrets.token_hex()}
            self.hass.config_entries.async_update_entry(self.entry, data=data)

        try:
            webhook_url = await self.get_url()
            data = {**self.entry.data, CONF_WEBHOOK_URL: webhook_url}
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        except NoURLAvailableError:
            _LOGGER.exception("Could not find base URL for installation")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "no_webhook_url",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="no_webhook_url",
            )
            return
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "no_webhook_url")

        if CONF_WEBHOOK_STATUS not in self.entry.data:
            self.async_show_setup_message(webhook_url)

        _LOGGER.info("Webhook registered at %s", webhook_url)

        # Ensure the webhook is not registered already
        webhook_unregister(self.hass, self.entry.data[CONF_WEBHOOK_ID])

        webhook_register(
            self.hass,
            DOMAIN,
            "TTLock",
            self.entry.data[CONF_WEBHOOK_ID],
            self.handle_webhook,
        )

        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self.unregister_webhook
        )

    async def handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Request
    ) -> None:
        """Handle webhook callback."""

        success = False
        try:
            # {'lockId': ['7252408'], 'notifyType': ['1'], 'records': ['[{"lockId":7252408,"electricQuantity":93,"serverDate":1680810180029,"recordTypeFromLock":17,"recordType":7,"success":1,"lockMac":"16:72:4C:CC:01:C4","keyboardPwd":"<digits>","lockDate":1680810186000,"username":"Jonas"}]'], 'admin': ['jonas@lemon.nz'], 'lockMac': ['16:72:4C:CC:01:C4']}
            if data := await request.post():
                _LOGGER.debug("Got webhook data: %s", data)
                for raw_records in data.getall("records", []):
                    for record in json.loads(raw_records):
                        async_dispatcher_send(
                            hass, SIGNAL_NEW_DATA, WebhookEvent.model_validate(record)
                        )
                        success = True
            else:
                _LOGGER.debug("handle_webhook, empty payload: %s", await request.text())
        except ValueError as ex:
            _LOGGER.exception("Exception parsing webhook data: %s", ex)
            return

        if success and CONF_WEBHOOK_STATUS not in self.entry.data:
            self.async_dismiss_setup_message()

    async def unregister_webhook(self, event: Event | None = None) -> None:
        """Remove the webhook (before stop)."""
        webhook_unregister(self.hass, self.entry.data[CONF_WEBHOOK_ID])

    def async_show_setup_message(self, uri: str) -> None:
        """Display persistent notification with setup information."""
        persistent_notification.async_create(
            self.hass,
            f"Webhook url: {uri}.\n\n"
            "This should be [registered with your app](https://open.ttlock.com/manager). "
            "See [the docs](https://github.com/jbergler/hass-ttlock?tab=readme-ov-file#creating-an-oauth-app) for more info.",
            "TTLock Setup",
            self.entry.entry_id,
        )

    def async_dismiss_setup_message(self) -> None:
        """Dismiss persistent notification."""
        data = {**self.entry.data, CONF_WEBHOOK_STATUS: True}
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        persistent_notification.async_dismiss(self.hass, self.entry.entry_id)
