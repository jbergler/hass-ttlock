"""The TTLock integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiohttp import ClientError, ClientResponseError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import (
    aiohttp_client,
    config_entry_oauth2_flow,
    issue_registry as ir,
)
from homeassistant.helpers.device_registry import DeviceEntry

from .api import RequestFailed, TTLockApi
from .capture import LockTrafficCapture
from .const import (
    CONF_CLOUD_ENABLED,
    CONF_REGION,
    CONF_WIFI_LOCAL_ENABLED,
    DEFAULT_CLOUD_ENABLED,
    DEFAULT_REGION,
    DEFAULT_WIFI_LOCAL_ENABLED,
    DOMAIN,
    TT_API,
    TT_CAPTURE,
    TT_GATEWAYS,
    TT_LOCKS,
    TT_STORE,
    TT_WIFI_PROXY,
    WIFI_PROXY_PORT,
)
from .coordinator import (
    GatewaysUpdateCoordinator,
    LockUpdateCoordinator,
    async_store_enumeration,
    cloud_unreachable_issue_id,
)
from .models import Features, LockSummary
from .services import Services
from .store import LockStateStore
from .webhook import WebhookHandler
from .wifi_proxy import WifiProxy

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
]

DETAIL_FILL_CONCURRENCY = 3

# Not an entry_id (those are opaque hex strings from HA's entry-id
# generator) - marks the one LockTrafficCapture shared by every loaded
# config entry, so a second TTLock account doesn't get a second buffer.
_CAPTURE_KEY = "_capture"

# Per-entry key holding the options snapshot the coordinators were built with,
# so _reload_on_options_update can ignore non-options entry updates.
_OPTIONS_KEY = "_options_snapshot"

# TTLock errcodes (docs/ttlock-cloud-api/errorCode.md) meaning the account's
# credentials themselves are bad - retrying setup won't fix these, so they start
# reauth. Every other errcode (90000 internal server error, 30006 rate limit,
# 80000 clock skew right after boot, ...) is treated as transient.
_AUTH_ERRCODES = frozenset({10000, 10001, 10003, 10004, 10007, 10011})

# HTTP statuses worth retrying; other 4xx (eg. a 400/401 from a rejected
# token refresh) mean the credentials need fixing, not that the cloud is down.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})

_LOGGER = logging.getLogger(__name__)


# What a failed key/list can raise: no network, an HTTP error, an unreadable
# body (ValueError), an errcode (RequestFailed) or a failed OAuth refresh.
_CLOUD_ERRORS = (ClientError, TimeoutError, ValueError, HomeAssistantError)


def _not_ready_reason(err: Exception) -> str | None:
    """Return why setup should be retried, or None if the credentials are bad.

    The reason is shown in the UI and logs, so it never includes str(err) -
    aiohttp's messages embed the request URL, accessToken and clientId included.
    """
    if isinstance(err, ConfigEntryAuthFailed):
        return None
    if isinstance(err, RequestFailed):
        if err.errcode in _AUTH_ERRCODES:
            return None
        return f"TTLock cloud returned error {err.errcode}"
    if isinstance(err, ClientResponseError):
        if err.status < 500 and err.status not in _TRANSIENT_HTTP_STATUSES:
            return None
        return f"TTLock cloud returned HTTP {err.status}"
    if isinstance(err, ValueError):
        return "TTLock cloud returned an unreadable response"
    return f"Unable to reach the TTLock cloud ({type(err).__name__})"


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

    # Re-create coordinators with the new cadence when polling options change.
    entry.async_on_unload(entry.add_update_listener(_reload_on_options_update))

    domain_data = hass.data.setdefault(DOMAIN, {})
    capture = domain_data.get(_CAPTURE_KEY)
    if capture is None:
        capture = LockTrafficCapture()
        domain_data[_CAPTURE_KEY] = capture

    store = domain_data.get(TT_STORE)
    if store is None:
        store = LockStateStore(hass)
        domain_data[TT_STORE] = store

    client = TTLockApi(
        aiohttp_client.async_get_clientsession(hass),
        session,
        capture,
        region=entry.data.get(CONF_REGION, DEFAULT_REGION),
    )
    cache = await store.async_get_entry(entry.entry_id)
    client.cached_uid = cache.get("uid", 0)
    cloud_enabled = entry.options.get(CONF_CLOUD_ENABLED, DEFAULT_CLOUD_ENABLED)

    summaries = await _async_discover(hass, entry, client, store, cache, cloud_enabled)

    locks = [
        LockUpdateCoordinator(hass, entry, client, summary, capture, store)
        for summary in summaries
    ]

    # Load the cached credentials and detail before any cloud fetch, so a lock
    # can be driven locally straight away and even if the cloud is gone.
    # Restore persisted credential names too, so local management shows real
    # names immediately (and even while the cloud is bypassed).
    await asyncio.gather(
        *(coordinator.async_restore_lock_cache() for coordinator in locks),
        *(coordinator.async_restore_credential_names() for coordinator in locks),
    )

    # With the cloud off the inbound webhook is severed too (see the
    # WebhookHandler.setup/async_disable branch below), so there is nothing for
    # the per-lock webhook listener to receive - skip wiring it up.
    for coordinator in locks:
        if cloud_enabled:
            entry.async_on_unload(coordinator.async_start_webhook_tracking())
        entry.async_on_unload(coordinator.async_start_ble_tracking())
        entry.async_on_unload(coordinator.async_start_local_refresh())

    # One wifi_proxy listener per HA instance, shared by the entries that opt
    # in: each registers its WiFi locks (MAC + credentials) so a connecting
    # lock can be matched and driven.
    if entry.options.get(CONF_WIFI_LOCAL_ENABLED, DEFAULT_WIFI_LOCAL_ENABLED):
        await _async_register_wifi_proxy(hass, entry, locks)

    # Gateways only matter to the cloud, so they never hold setup up: a failed
    # first refresh leaves the cached list in place, marked unavailable.
    gateway_coordinator: GatewaysUpdateCoordinator | None = None
    if cloud_enabled:
        gateway_coordinator = GatewaysUpdateCoordinator(hass, entry, client, store)
        gateway_coordinator.async_restore(cache.get("gateways", []))
        await gateway_coordinator.async_refresh()

    # Only published once discovery succeeded, so an entry waiting in
    # SETUP_RETRY doesn't leave half-initialized runtime data behind for
    # async_remove_config_entry_device / async_unload_entry to trip over.
    domain_data[entry.entry_id] = {
        TT_API: client,
        TT_CAPTURE: capture,
        TT_LOCKS: locks,
        TT_GATEWAYS: gateway_coordinator,
        # Snapshot of the options the coordinators above were built with, so
        # the update listener can tell a real options change from the entry.data
        # writes webhook.py makes during setup (which must not trigger a reload).
        _OPTIONS_KEY: dict(entry.options),
    }

    webhook_handler = WebhookHandler(
        hass,
        entry,
        capture,
        implementation.client_id,  # ty: ignore[unresolved-attribute] - implementation is a TTLockAuthImplementation
    )
    if cloud_enabled:
        await webhook_handler.setup()
    else:
        # Cloud off: unregister the inbound webhook too, so the TTLock cloud
        # can't reach HA either. Re-enabling reloads the entry and setup()
        # above registers it again. Best-effort, like setup()'s own
        # _register_webhook_safely - it must never fail entry setup (which
        # would strand the lock's local transports too).
        try:
            await webhook_handler.async_disable()
        except Exception:
            # Severing inbound is best-effort - it must never fail entry setup.
            _LOGGER.exception("Failed to disable the inbound webhook (cloud off)")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if skipped := [coordinator for coordinator in locks if not coordinator.connectable]:
        _LOGGER.warning(
            "%d lock(s) have no gateway or WiFi and are not in Bluetooth range; "
            "they will show as unavailable until one of those changes: %s",
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


async def _async_discover(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: TTLockApi,
    store: LockStateStore,
    cache: dict,
    cloud_enabled: bool,
) -> list[LockSummary]:
    """The entry's locks: from key/list, or from the cache when that fails.

    With the cloud off the cache is used without asking the cloud; only an
    entry that has never enumerated makes this one call. A failure with a
    cache still loads the entry (raising a repair, and starting reauth when the
    credentials were refused); without one, a transient failure retries setup
    and refused credentials start reauth.
    """
    cached = [LockSummary.model_validate(r) for r in cache.get("key_records", [])]
    if cached and not cloud_enabled:
        return cached
    # Also the call that refreshes the OAuth token if needed.
    try:
        summaries = await client.get_locks()
    except _CLOUD_ERRORS as err:
        reason = _not_ready_reason(err)
        if not cached:
            if reason is None:
                raise ConfigEntryAuthFailed("TTLock refused the credentials") from err
            raise ConfigEntryNotReady(reason) from err
        _LOGGER.warning(
            "TTLock cloud unavailable (%s); starting %d lock(s) from the cache",
            reason or "credentials refused",
            len(cached),
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            cloud_unreachable_issue_id(entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="cloud_unreachable",
            translation_placeholders={"entry_title": entry.title},
        )
        if reason is None:
            entry.async_start_reauth(hass)
        return cached
    await async_store_enumeration(hass, store, entry.entry_id, summaries, client.uid)
    return summaries or cached


def _mac_bytes(mac: str) -> bytes | None:
    """Turn a lock's AA:BB:.. MAC string into 6 raw bytes, or None if malformed."""
    try:
        raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    except ValueError:
        return None
    return raw if len(raw) == 6 else None


async def _async_register_wifi_proxy(
    hass: HomeAssistant, entry: ConfigEntry, locks: list[LockUpdateCoordinator]
) -> None:
    """Register this entry's WiFi locks with the shared proxy and start it."""
    wifi_locks = [
        (coordinator, mac)
        for coordinator in locks
        if Features.wifi in coordinator.data.features
        and (mac := _mac_bytes(coordinator.data.mac)) is not None
    ]
    if not wifi_locks:
        return
    domain_data = hass.data.setdefault(DOMAIN, {})
    proxy: WifiProxy | None = domain_data.get(TT_WIFI_PROXY)
    if proxy is None:
        proxy = WifiProxy()
        domain_data[TT_WIFI_PROXY] = proxy

    registrations: list[tuple[int, object]] = []
    for coordinator, mac in wifi_locks:
        handle = proxy.register_lock(
            coordinator.lock_id,
            mac,
            (lambda c=coordinator: c.cloud_enabled),
            coordinator.wifi_credentials,
            coordinator.async_wifi_local_changed,
            coordinator.async_wifi_local_event,
        )
        registrations.append((coordinator.lock_id, handle))
    entry.async_on_unload(lambda: _async_release_wifi_proxy(hass, registrations))

    if await proxy.async_start():
        ir.async_delete_issue(hass, DOMAIN, "wifi_proxy_bind_failed")
    else:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "wifi_proxy_bind_failed",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="wifi_proxy_bind_failed",
            translation_placeholders={"port": str(WIFI_PROXY_PORT)},
        )


async def _async_release_wifi_proxy(
    hass: HomeAssistant, registrations: list[tuple[int, object]]
) -> None:
    """Drop an entry's locks from the shared proxy; stop it once none are left.

    Decided by the proxy's own registrations, not by which entries are loaded,
    so a sibling entry mid-setup keeps the locks it already registered.
    """
    domain_data = hass.data.get(DOMAIN, {})
    proxy: WifiProxy | None = domain_data.get(TT_WIFI_PROXY)
    if proxy is None:
        return
    for lock_id, handle in registrations:
        proxy.unregister_lock(lock_id, handle)
    if not proxy.has_registrations:
        del domain_data[TT_WIFI_PROXY]
        ir.async_delete_issue(hass, DOMAIN, "wifi_proxy_bind_failed")
        await proxy.async_stop()


async def _reload_on_options_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change, so coordinators pick them up.

    HA fires this listener on any entry update, including the entry.data writes
    webhook.py makes during setup - reload only when the options themselves
    actually changed, or those writes would trigger a reload storm mid-setup.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    # No snapshot yet (update fired before setup finished storing it, or the
    # entry isn't loaded) - not a real options change, so don't reload.
    if entry_data is None or entry_data.get(_OPTIONS_KEY) == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


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
            domain_data.pop(TT_STORE, None)

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
    if (gateways := domain_data[TT_GATEWAYS]) is not None:
        known_macs.update(gateway.mac for gateway in (gateways.data or {}).values())

    return not any((DOMAIN, mac) in device_entry.identifiers for mac in known_macs)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Purge the entry's cache; delete its cloudhook once no sibling shares it.

    Sibling config entries under the same client_id share one webhook_id
    (see webhook.py's module docstring), so deleting on every removal would
    pull the rug out from under any other entry still using it - checking
    entry.data[CONF_WEBHOOK_ID] against every other loaded/configured entry
    (already kept in sync by webhook.py's _sync_entry_to_group) avoids
    needing this entry's OAuth2 implementation, which may no longer be
    resolvable this late in removal.
    """
    store = hass.data.get(DOMAIN, {}).get(TT_STORE) or LockStateStore(hass)
    await store.async_remove_entry(entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, cloud_unreachable_issue_id(entry.entry_id))

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
