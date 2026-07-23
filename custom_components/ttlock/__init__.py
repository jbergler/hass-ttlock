"""The TTLock integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .api import TTLockApi
from .const import DOMAIN, TT_API, TT_GATEWAYS, TT_LOCKS
from .coordinator import GatewaysUpdateCoordinator, LockUpdateCoordinator
from .services import Services
from .webhook import WebhookHandler

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

DETAIL_FILL_CONCURRENCY = 3

_LOGGER = logging.getLogger(__name__)


def setup(hass: HomeAssistant, config: ConfigEntry) -> bool:
    """Set up the TTLock component."""

    Services(hass).register()

    return True


async def _fill_lock_details(locks: list[LockUpdateCoordinator]) -> None:
    """Progressively fetch full per-lock detail in the background after setup.

    Bounded concurrency so many locks don't all hit the TTLock cloud API at
    once - this is about smooth progressive loading, not rate-limiting.
    """
    semaphore = asyncio.Semaphore(DETAIL_FILL_CONCURRENCY)

    async def _refresh_one(coordinator: LockUpdateCoordinator) -> None:
        async with semaphore:
            await coordinator.async_refresh()

    await asyncio.gather(*(_refresh_one(coordinator) for coordinator in locks))


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
        LockUpdateCoordinator(hass, entry, client, summary)
        for summary in await client.get_locks()
    ]
    hass.data[DOMAIN][entry.entry_id][TT_LOCKS] = locks

    gateway_coordinator = GatewaysUpdateCoordinator(hass, entry, client)
    await gateway_coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id][TT_GATEWAYS] = gateway_coordinator

    await WebhookHandler(hass, entry).setup()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if skipped := [coordinator for coordinator in locks if not coordinator.connectable]:
        _LOGGER.warning(
            "%d lock(s) have no gateway or WiFi and will show as unavailable "
            "until connectivity is restored: %s",
            len(skipped),
            ", ".join(
                f"{coordinator.data.name} ({coordinator.lock_id})"
                for coordinator in skipped
            ),
        )

    connectable_locks = [
        coordinator for coordinator in locks if coordinator.connectable
    ]
    entry.async_create_background_task(
        hass,
        _fill_lock_details(connectable_locks),
        name=f"ttlock-{entry.entry_id}-initial-detail-fill",
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
