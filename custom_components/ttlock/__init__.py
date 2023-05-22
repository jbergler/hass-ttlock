"""The TTLock integration."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets

from aiohttp.web import Request

from homeassistant.components import cloud, persistent_notification, webhook
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
from .const import (
    CONF_WEBHOOK_STATUS,
    CONF_WEBHOOK_URL,
    DOMAIN,
    SIGNAL_NEW_DATA,
    TT_API,
    TT_LOCKS,
)
from .coordinator import LockUpdateCoordinator
from .models import WebhookEvent
from .services import Services

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.SENSOR, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


def setup(hass: HomeAssistant, config: ConfigEntry) -> bool:
    """Set up the TTLock component."""

    Services(hass).register()

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TTLock from a config entry."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )

    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    client = TTLockApi(aiohttp_client.async_get_clientsession(hass), session)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {TT_API: client}

    locks = [
        LockUpdateCoordinator(hass, client, lock_id)
        for lock_id in await client.get_locks()
    ]
    await asyncio.gather(
        *[coordinator.async_config_entry_first_refresh() for coordinator in locks]
    )
    hass.data[DOMAIN][entry.entry_id][TT_LOCKS] = locks

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
                            hass, SIGNAL_NEW_DATA, WebhookEvent.parse_obj(record)
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
            self.hass, f"Webhook url: {uri}", "TTLock Setup", self.entry.entry_id
        )

    def async_dismiss_setup_message(self) -> None:
        """Dismiss persistent notification."""
        data = {**self.entry.data, CONF_WEBHOOK_STATUS: True}
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        persistent_notification.async_dismiss(self.hass, self.entry.entry_id)
