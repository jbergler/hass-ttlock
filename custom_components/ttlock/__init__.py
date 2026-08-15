"""The TTLock integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow
from homeassistant.helpers.device_registry import DeviceEntry

from .api import TTLockApi
from .capture import LockTrafficCapture
from .const import (
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
    TT_API,
    TT_CAPTURE,
    TT_GATEWAYS,
    TT_LOCKS,
)
from .coordinator import GatewaysUpdateCoordinator, LockUpdateCoordinator
from .services import Services
from .store import LockStateStore
from .webhook import WebhookHandler

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

DETAIL_FILL_CONCURRENCY = 3

# Not an entry_id (those are opaque hex strings from HA's entry-id
# generator) - marks the one LockTrafficCapture shared by every loaded
# config entry, so a second TTLock account doesn't get a second buffer.
_CAPTURE_KEY = "_capture"

# Same pattern as _CAPTURE_KEY: one LockStateStore shared by every loaded
# config entry, since it's a single JSON file keyed by lock ID, not
# per-account state.
_STORE_KEY = "_lock_state_store"

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

    domain_data = hass.data.setdefault(DOMAIN, {})
    capture = domain_data.get(_CAPTURE_KEY)
    if capture is None:
        capture = LockTrafficCapture()
        domain_data[_CAPTURE_KEY] = capture

    store = domain_data.get(_STORE_KEY)
    if store is None:
        store = LockStateStore(hass)
        domain_data[_STORE_KEY] = store

    client = TTLockApi(
        aiohttp_client.async_get_clientsession(hass),
        session,
        capture,
        region=entry.data.get(CONF_REGION, DEFAULT_REGION),
    )

    domain_data[entry.entry_id] = {
        TT_API: client,
        TT_CAPTURE: capture,
    }

    locks = [
        LockUpdateCoordinator(hass, entry, client, summary, capture, store)
        for summary in await client.get_locks()
    ]
    hass.data[DOMAIN][entry.entry_id][TT_LOCKS] = locks

    gateway_coordinator = GatewaysUpdateCoordinator(hass, entry, client)
    await gateway_coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id][TT_GATEWAYS] = gateway_coordinator

    await WebhookHandler(
        hass,
        entry,
        capture,
        implementation.client_id,  # ty: ignore[unresolved-attribute] - implementation is a TTLockAuthImplementation
    ).setup()

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
        domain_data = hass.data[DOMAIN]
        domain_data.pop(entry.entry_id)

        # last entry gone - drop the shared capture buffer too. Entry IDs are
        # hex strings from HA's entry-id generator; shared, non-entry state
        # (this module's _CAPTURE_KEY, webhook.py's own group bookkeeping) is
        # marked with a leading underscore so it doesn't count as "an entry
        # is still loaded" here.
        if not any(not key.startswith("_") for key in domain_data):
            domain_data.pop(_CAPTURE_KEY, None)
            domain_data.pop(_STORE_KEY, None)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow manually deleting a lock/gateway device once it's gone from the account.

    A lock or gateway removed from the TTLock account drops out of
    async_setup_entry's next get_locks()/get_gateways() call, but its device
    stays behind in the registry with no entities - the only way to clear it
    is a manual delete from the device page. Block that for macs still
    present in this entry's live coordinators (a still-active device HA would
    just recreate); allow it otherwise, including when this entry's runtime
    data isn't in hass.data at all (failed/unloaded entry - nothing to check
    against, and exactly when stale devices are likely to need clearing).
    """
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if domain_data is None:
        return True

    known_macs = {coordinator.data.mac for coordinator in domain_data[TT_LOCKS]}
    gateways = domain_data[TT_GATEWAYS].data or {}
    known_macs.update(gateway.mac for gateway in gateways.values())

    return not any((DOMAIN, mac) in device_entry.identifiers for mac in known_macs)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's cloudhook, once no sibling entry still shares it.

    Sibling config entries under the same client_id share one webhook_id
    (see webhook.py's module docstring), so deleting on every removal would
    pull the rug out from under any other entry still using it - checking
    entry.data[CONF_WEBHOOK_ID] against every other loaded/configured entry
    (already kept in sync by webhook.py's _sync_entry_to_group) avoids
    needing this entry's OAuth2 implementation, which may no longer be
    resolvable this late in removal.
    """
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id is None:
        return

    still_shared = any(
        candidate.entry_id != entry.entry_id
        and candidate.data.get(CONF_WEBHOOK_ID) == webhook_id
        for candidate in hass.config_entries.async_entries(DOMAIN)
    )
    if still_shared:
        return

    # deferred: cloud pulls in optional heavy dependencies we don't want to
    # require at module import time - matches webhook.py's try_generate_cloudhook
    from homeassistant.components import cloud  # noqa: PLC0415

    if not cloud.async_active_subscription(hass):
        return

    # ValueError alongside CloudNotAvailable: hass_nabucasa's Cloudhooks.async_delete
    # raises a bare ValueError if this webhook_id was never actually converted to a
    # cloudhook (eg. try_generate_cloudhook returned None despite an active
    # subscription) - matches mobile_app's async_remove_entry, which hits the same gap.
    with contextlib.suppress(cloud.CloudNotAvailable, ValueError):
        await cloud.async_delete_cloudhook(hass, webhook_id)
