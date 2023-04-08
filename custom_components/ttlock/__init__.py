"""The TTLock integration."""
from __future__ import annotations

import json
import logging
import secrets

from aiohttp.web import Request

from homeassistant.components import cloud, webhook
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_WEBHOOK_ID,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import TTLockApi
from .const import CONF_WEBHOOK_URL, DOMAIN, SIGNAL_NEW_DATA, TT_API, TT_LOCKS

PLATFORMS: list[Platform] = [Platform.LOCK]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TTLock from a config entry."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )

    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        TT_API: TTLockApi(aiohttp_client.async_get_clientsession(hass), session),
        TT_LOCKS: [],
    }

    await WebhookHandler(hass, entry).setup()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


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

    async def register_webhook(self, event: Event | None = None) -> None:
        """Set up a webhook to receive pushed data."""
        if CONF_WEBHOOK_ID not in self.entry.data:
            _LOGGER.info("Webhook not found in config entry, creating new one")
            data = {**self.entry.data, CONF_WEBHOOK_ID: secrets.token_hex()}
            self.hass.config_entries.async_update_entry(self.entry, data=data)

        if cloud.async_active_subscription(self.hass):
            if CONF_WEBHOOK_URL not in self.entry.data:
                try:
                    webhook_url = await cloud.async_create_cloudhook(
                        self.hass, self.entry.data[CONF_WEBHOOK_ID]
                    )
                except cloud.CloudNotConnected:
                    webhook_url = webhook.async_generate_url(
                        self.hass, self.entry.data[CONF_WEBHOOK_ID]
                    )
                else:
                    data = {**self.entry.data, CONF_WEBHOOK_URL: webhook_url}
                    self.hass.config_entries.async_update_entry(self.entry, data=data)
            else:
                webhook_url = self.entry.data[CONF_WEBHOOK_URL]
        else:
            webhook_url = webhook.async_generate_url(
                self.hass, self.entry.data[CONF_WEBHOOK_ID]
            )

        _LOGGER.info(f"Webhook {webhook_url}")

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
        # lockId=7252408&notifyType=1&records=%5B%7B%22lockId%22%3A7252408%2C%22electricQuantity%22%3A93%2C%22serverDate%22%3A1680810180029%2C%22recordTypeFromLock%22%3A17%2C%22recordType%22%3A7%2C%22success%22%3A1%2C%22lockMac%22%3A%2216%3A72%3A4C%3ACC%3A01%3AC4%22%2C%22keyboardPwd%22%3A%221292195345%22%2C%22lockDate%22%3A1680810186000%2C%22username%22%3A%22Jonas%22%7D%5D&admin=jonas%40lemon.nz&lockMac=16%3A72%3A4C%3ACC%3A01%3AC4
        # {'lockId': ['7252408'], 'notifyType': ['1'], 'records': ['[{"lockId":7252408,"electricQuantity":93,"serverDate":1680810180029,"recordTypeFromLock":17,"recordType":7,"success":1,"lockMac":"16:72:4C:CC:01:C4","keyboardPwd":"1292195345","lockDate":1680810186000,"username":"Jonas"}]'], 'admin': ['jonas@lemon.nz'], 'lockMac': ['16:72:4C:CC:01:C4']}
        try:
            data = await request.post()
            if data:
                for v in data.getall("records", []):
                    for record in json.loads(v):
                        async_dispatcher_send(hass, SIGNAL_NEW_DATA, record)
            else:
                _LOGGER.debug(f"handle_webhook, empty payload: {await request.text()}")
        except ValueError:
            return

        _LOGGER.debug("Got webhook data: %s", data)

    async def unregister_webhook(self, event: Event | None = None) -> None:
        """Remove the webhook (before stop)."""
        webhook_unregister(self.hass, self.entry.data[CONF_WEBHOOK_ID])
