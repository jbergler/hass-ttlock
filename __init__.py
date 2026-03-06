"""The TTLock integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

# 🔧 FIX: imports absolutos (pytest no soporta imports relativos)
from custom_components.ttlock.api import TTLockApi
from custom_components.ttlock.const import DOMAIN, TT_API, TT_LOCKS
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.services import Services
from custom_components.ttlock.webhook import WebhookHandler

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

_LOCK_LIST_TIMEOUT = 15  # seconds


def setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up TTLock (YAML)."""
    Services(hass).register()
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TTLock from a config entry."""
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    client = TTLockApi(aiohttp_client.async_get_clientsession(hass), session)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {TT_API: client, TT_LOCKS: []}

    try:
        lock_ids = await asyncio.wait_for(client.get_locks(), timeout=_LOCK_LIST_TIMEOUT)
    except (TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(
            f"Timeout/unavailable while fetching TTLock lock list ({_LOCK_LIST_TIMEOUT}s)"
        ) from err
    except Exception as err:
        raise ConfigEntryNotReady("TTLock not ready while fetching lock list") from err

    coordinators: list[LockUpdateCoordinator] = [
        LockUpdateCoordinator(hass, entry, client, lock_id) for lock_id in lock_ids
    ]

    hass.data[DOMAIN][entry.entry_id][TT_LOCKS] = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _setup_webhook() -> None:
        try:
            await WebhookHandler(hass, entry).setup()
        except Exception:
            _LOGGER.exception("Failed to set up TTLock webhook")

    hass.async_create_task(_setup_webhook(), name=f"ttlock_webhook_{entry.entry_id}")

    async def _first_refresh(coordinator: LockUpdateCoordinator) -> None:
        try:
            await coordinator.async_config_entry_first_refresh()
        except Exception:
            _LOGGER.exception(
                "TTLock first refresh failed for %s", coordinator.unique_id
            )

    for coordinator in coordinators:
        hass.async_create_task(
            _first_refresh(coordinator),
            name=f"ttlock_first_refresh_{entry.entry_id}_{coordinator.lock_id}",
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
