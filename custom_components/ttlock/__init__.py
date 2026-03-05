"""The TTLock integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .api import TTLockApi
from .const import DOMAIN, TT_API, TT_LOCKS
from .coordinator import LockUpdateCoordinator
from .services import Services
from .webhook import WebhookHandler

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

# Keep startup responsive: do not block Home Assistant indefinitely on cloud calls.
_LOCK_LIST_TIMEOUT = 15  # seconds
_FIRST_REFRESH_TIMEOUT = 25  # seconds (total budget for initial refresh)


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

    # 1) Discover locks with a hard timeout so startup can't hang forever.
    try:
        lock_ids = await asyncio.wait_for(client.get_locks(), timeout=_LOCK_LIST_TIMEOUT)
    except (asyncio.TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(
            f"Timeout/unavailable while fetching TTLock lock list ({_LOCK_LIST_TIMEOUT}s)"
        ) from err
    except Exception as err:
        raise ConfigEntryNotReady("TTLock not ready while fetching lock list") from err

    coordinators: list[LockUpdateCoordinator] = [
        LockUpdateCoordinator(hass, entry, client, lock_id) for lock_id in lock_ids
    ]

    # 2) IMPORTANT: Entities access coordinator.data during their __init__ (device_info).
    #    So we must complete at least one refresh before forwarding platforms.
    async def _first_refresh_all() -> None:
        results = await asyncio.gather(
            *(c.async_config_entry_first_refresh() for c in coordinators),
            return_exceptions=True,
        )
        # Drop coordinators that never produced data; keep working ones.
        ok: list[LockUpdateCoordinator] = []
        for c, r in zip(coordinators, results, strict=False):
            if isinstance(r, Exception):
                _LOGGER.debug("TTLock first refresh failed for %s: %r", getattr(c, "name", "lock"), r)
                continue
            if getattr(c, "data", None) is None:
                continue
            ok.append(c)
        coordinators[:] = ok

    try:
        await asyncio.wait_for(_first_refresh_all(), timeout=_FIRST_REFRESH_TIMEOUT)
    except asyncio.TimeoutError as err:
        # If nothing loaded, ask HA to retry later.
        raise ConfigEntryNotReady(
            f"Timeout while performing TTLock first refresh ({_FIRST_REFRESH_TIMEOUT}s)"
        ) from err

    if not coordinators:
        raise ConfigEntryNotReady("TTLock first refresh returned no usable lock data")

    hass.data[DOMAIN][entry.entry_id][TT_LOCKS] = coordinators

    # 3) Set up webhook (non-fatal).
    try:
        await WebhookHandler(hass, entry).setup()
    except Exception:
        _LOGGER.exception("Failed to set up TTLock webhook")

    # 4) Now it's safe to forward platforms.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
