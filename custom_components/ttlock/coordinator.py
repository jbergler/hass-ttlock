"""Provides the TTLock LockUpdateCoordinator.

State reaches HA two ways: polling and webhook push, both merging into
LockUpdateCoordinator. This module owns polling — LockUpdateCoordinator (per
lock, on the configurable fast and slow tiers) and GatewaysUpdateCoordinator
(gateway online/offline, every 15 minutes). LockUpdateCoordinator also listens
for SIGNAL_NEW_DATA
(dispatched from webhook.py) via _process_webhook_data, which is why lock
state updates are close to real-time rather than poll-only — don't remove
the webhook path in favor of "just poll faster."
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import (
    contextmanager,
    suppress,
)
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any, TypeGuard

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TTLockApi
from .ble import (
    BleData,
    BleError,
    async_bluetooth_available,
    async_control,
    async_read_status,
    async_track_address,
)
from .ble_protocol import (
    LockStatus,
    LockVersion,
    ProtocolError,
    decode_password,
    parse_aes_key,
)
from .capture import LockTrafficCapture
from .const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_SLOW_POLL_INTERVAL,
    CONF_TRANSPORT_ORDER,
    DEFAULT_BLE_CONTROL,
    DEFAULT_BLUETOOTH_ENABLED,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_SLOW_POLL_INTERVAL_HOURS,
    DEFAULT_TRANSPORT_ORDER,
    DOMAIN,
    SIGNAL_NEW_DATA,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TT_GATEWAYS,
    TT_LOCKS,
)
from .models import (
    AccessLevel,
    Features,
    Gateway,
    GatewayLink,
    Lock,
    LockDetail,
    LockSummary,
    OnOff,
    PassageModeConfig,
    Sensor,
    SensorState,
    State,
    WebhookEvent,
)
from .store import LockStateStore

_LOGGER = logging.getLogger(__name__)


NOT_CONNECTABLE_REASON = (
    "No gateway paired, no WiFi, and not in Bluetooth range of Home Assistant - "
    "nothing can reach this lock. Live data isn't available until one of those "
    "changes."
)


# A lock the radio can still hear rides out this many failed state reads,
# retried at this cadence, before it goes unavailable and the poll returns
# to its normal interval.
RETRY_INTERVAL = timedelta(minutes=1)
REVERIFY_GRACE = 2

# A lock only the radio can reach has no webhook, so between polls nothing
# would notice a manual turn. Each refresh is a connect and costs the lock's
# battery, hence minutes rather than seconds.
LOCAL_REFRESH_INTERVAL = timedelta(minutes=5)

# TTLock's doorSensor/query can't distinguish "no sensor paired" from a
# transient failure (see api.get_sensor), so a lock only counts as confirmed
# absent after this many consecutive failed checks - see
# LockUpdateCoordinator._check_for_sensor.
SENSOR_ABSENT_AFTER_FAILURES = 3


def cloud_unreachable_issue_id(entry_id: str) -> str:
    """The repair issue raised while an entry runs on its cached key records."""
    return f"cloud_unreachable_{entry_id}"


async def async_store_enumeration(
    hass: HomeAssistant,
    store: LockStateStore,
    entry_id: str,
    summaries: list[LockSummary],
    uid: int = 0,
) -> None:
    """Keep a successful key/list (and the uid) for a setup without the cloud.

    An empty list is not kept: it would erase the only way to start offline.
    """
    ir.async_delete_issue(hass, DOMAIN, cloud_unreachable_issue_id(entry_id))
    if not summaries:
        return
    records = [summary.model_dump(mode="json", by_alias=True) for summary in summaries]
    await store.async_update_entry(
        entry_id, key_records=records, **({"uid": uid} if uid else {})
    )


@dataclass
class SensorData:
    """Internal state of the optional door sensor."""

    opened: bool | None = None
    battery: int | None = None
    last_fetched: datetime | None = None

    @property
    def present(self) -> bool:
        """To indicate if a sensor is installed."""
        return self.battery is not None


@dataclass
class LockState:
    """Internal state of the lock as managed by the co-oridinator."""

    name: str
    mac: str
    model: str | None = None
    battery_level: int | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    features: Features = field(default_factory=lambda: Features(0))
    locked: bool | None = None
    action_pending: bool = False
    last_user: str | None = None
    last_reason: str | None = None
    lock_sound: bool | None = None
    sensor: SensorData | None = None
    auto_lock_seconds: int | None = None
    passage_mode_config: PassageModeConfig | None = None
    gateways: list[GatewayLink] = field(default_factory=list)

    @property
    def best_gateway(self) -> GatewayLink | None:
        """The gateway currently used to reach the lock, by best RSSI."""
        return self.gateways[0] if self.gateways else None

    def passage_mode_active(self, current_date: datetime | None = None) -> bool:
        """Check if passage mode is currently active."""
        if current_date is None:
            current_date = dt_util.now()
        if self.passage_mode_config and self.passage_mode_config.enabled:
            current_day = current_date.isoweekday()

            if current_day in self.passage_mode_config.week_days:
                if self.passage_mode_config.all_day:
                    return True

                current_minute = current_date.hour * 60 + current_date.minute
                if (
                    self.passage_mode_config.start_minute
                    <= current_minute
                    < self.passage_mode_config.end_minute
                ):
                    # Active by schedule
                    return True
        return False

    def auto_lock_delay(self, current_date: datetime) -> int | None:
        """Return the auto-lock delay in seconds, or None if auto-lock is currently disabled."""
        if self.auto_lock_seconds is None or self.auto_lock_seconds <= 0:
            return None

        if self.passage_mode_active(current_date):
            return None

        return self.auto_lock_seconds


def sensor_present(instance: SensorData | None) -> TypeGuard[SensorData]:
    """Check if a door sensor accessory is paired."""
    return instance is not None and instance.present


def door_state_known(instance: SensorData | None) -> TypeGuard[SensorData]:
    """Check if a door state is available, whatever reported it.

    A lock with a built-in door contact reports the door over the radio but
    has no accessory for doorSensor/query to describe, so an accessory is the
    wrong thing to wait for before publishing a door state.
    """
    return instance is not None and (instance.present or instance.opened is not None)


def async_add_when_sensor_present(
    coordinator: LockUpdateCoordinator,
    add_entities: Callable[[], None],
    ready: Callable[[SensorData | None], bool] = sensor_present,
) -> None:
    """Call `add_entities` once `ready` holds - now, or later.

    Door-sensor presence isn't known at the cheap lock/list stage entities
    are created from at platform setup - it's only filled in once the
    coordinator's first refresh completes, which __init__.py runs in the
    background *after* platforms are set up (see _fill_lock_details), so
    sensor-backed entities can't just check coordinator.data.sensor at setup
    time. Instead: add immediately if `ready` already holds, otherwise watch
    for the update that makes it true and add then.
    """
    if ready(coordinator.data.sensor):
        add_entities()
        return

    remove: Callable[[], None] | None = None

    @callback
    def _check_and_add() -> None:
        if ready(coordinator.data.sensor):
            add_entities()
            if remove is not None:
                remove()

    remove = coordinator.async_add_listener(_check_and_add)


@contextmanager
def lock_action(controller: LockUpdateCoordinator):
    """Wrap a lock action so that in-progress state is managed correctly."""

    controller.data.action_pending = True
    controller.async_update_listeners()
    try:
        yield
    finally:
        controller.data.action_pending = False
        controller.async_update_listeners()


def lock_coordinators(hass: HomeAssistant, entry: ConfigEntry):
    """Help with entity setup."""
    coordinators: list[LockUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id][
        TT_LOCKS
    ]
    yield from coordinators


def gateway_coordinator(
    hass: HomeAssistant, entry: ConfigEntry
) -> GatewaysUpdateCoordinator:
    """Get the gateway coordinator."""
    return hass.data[DOMAIN][entry.entry_id][TT_GATEWAYS]


def coordinator_for(
    hass: HomeAssistant, entity_id: str
) -> LockUpdateCoordinator | None:
    """Given an entity_id, return the coordinator for that entity."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        for coordinator in lock_coordinators(hass, entry):
            for entity in coordinator.entities:
                if entity.entity_id == entity_id:
                    return coordinator
    return None


class LockUpdateCoordinator(DataUpdateCoordinator[LockState]):
    """Class to manage fetching Toon data from single endpoint."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: TTLockApi,
        summary: LockSummary,
        capture: LockTrafficCapture,
        store: LockStateStore,
    ) -> None:
        """Initialize the update co-ordinator for a single lock."""
        self.api = api
        self.lock_id = summary.id
        self.has_gateway = summary.hasGateway
        self.feature_value = summary.featureValue
        # Live v3/key/list record. Properties below forward into it; the
        # slow tier swaps it via _maybe_refresh_summary so admin-side
        # changes (freeze, remote toggle, validity edits) take effect
        # within a poll cycle.
        self.summary = summary
        # Cloud reachability is decided once at discovery; the Bluetooth
        # half of `connectable` is reconciled at runtime.
        self.cloud_connectable = summary.connectable
        self._capture = capture
        self._store = store

        # Latest passive Bluetooth presence for this lock.
        # Empty until async_start_ble_tracking hears the first
        # advertisement (or immediately, if the stack already has one)
        self.ble: BleData = BleData()
        self._ble_verified_at: datetime | None = None
        self._reverify_failures = 0
        # When a state read last landed, whatever it said. A lock that answers
        # "unknown" publishes that, so the state itself can no longer stand in
        # for "we have never managed to read this lock".
        self._verified_at: datetime | None = None

        # What lock/detail told us about talking to the lock directly. Kept
        # as received; parsed on use so an odd value costs a local read, not
        # the poll.
        self._ble_version: dict[str, int] | None = None
        self._ble_key: str | None = None
        # Extra credentials needed to CHANGE state (not just read it): the
        # obfuscated per-key secret and the key's flag generation. Decoded on
        # use, like the AES key.
        self._ble_unlock_key: str | None = None
        self._ble_lock_flag_pos: int = 0

        # Whether we've already made this restart's one-shot door-sensor
        # recheck (see _check_for_sensor) - deliberately in-memory, not
        # persisted, so it naturally resets to False on every restart.
        self._sensor_recheck_done = False

        # The pending "assume auto-locked" timer started by the last unlock, if
        # any - kept so a newer unlock (or unload) can cancel it.
        self._auto_lock_task: asyncio.Task | None = None

        # The Bluetooth link: one GATT conversation with the lock at a time, so
        # a state read and a lock/unlock never interleave.
        self._local_lock = asyncio.Lock()

        # Slow-tier cadence and its per-call "last fetched" gates. Only lock
        # state is re-verified every poll; detail/passage/gateway fetches run
        # at most once per _slow_interval (see _async_update_data). Timestamps
        # are in-memory, so a restart re-fetches everything once - which is
        # what we want on startup anyway.
        options = config_entry.options if config_entry else {}
        poll_minutes = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_MINUTES)
        slow_hours = options.get(
            CONF_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL_HOURS
        )
        self._poll_interval = timedelta(minutes=poll_minutes)
        self._slow_interval = timedelta(hours=slow_hours)
        self._ble_option: bool = options.get(
            CONF_BLUETOOTH_ENABLED, DEFAULT_BLUETOOTH_ENABLED
        )
        self._ble_control_option: bool = options.get(
            CONF_BLE_CONTROL, DEFAULT_BLE_CONTROL
        )
        self._transport_order: list[str] = list(
            options.get(CONF_TRANSPORT_ORDER, DEFAULT_TRANSPORT_ORDER)
        )
        self._details_last_fetched: datetime | None = None
        self._passage_last_fetched: datetime | None = None
        self._gateways_last_fetched: datetime | None = None

        # The transport that last successfully drove the bolt (BLE / cloud),
        # surfaced by the connection diagnostics. None until the first
        # lock/unlock lands.
        self._last_transport: str | None = None

        # A successful local read vouches for the lock this long - longer
        # than the poll, so a healthy lock isn't unproven while waiting for
        # the poll that would confirm it.
        self._proven_reachable_for = 2 * self._poll_interval

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=self._poll_interval if self.connectable else None,
        )

        self.data = LockState(
            name=summary.name, mac=summary.mac, features=summary.features
        )
        # Ekey record refresh: in-memory shared cache + gate. Shared across
        # every coordinator for the same account via hass.data, so one
        # account-wide v3/key/list call per slow interval feeds all locks.
        self._summary_last_fetched: datetime | None = None

    async def _maybe_refresh_summary(self, now: datetime) -> None:
        """Pull this lock's ekey record from the shared cache if due."""
        if not self._slow_tier_due(self._summary_last_fetched, now):
            return
        for candidate in await self._shared_ekey_list(now):
            if candidate.id == self.lock_id:
                self.summary = candidate
                break
        self._summary_last_fetched = now

    async def _shared_ekey_list(self, now: datetime) -> list[LockSummary]:
        """One v3/key/list fetch per config entry per slow interval.

        Keyed on the entry_id (not id(api), which is recyclable after a
        reload) and single-flighted through an asyncio.Lock so concurrent
        coordinators from the same entry wait for the first fetch instead
        of each triggering their own.
        """
        assert self.config_entry is not None
        bucket = self.hass.data.setdefault(DOMAIN, {}).setdefault("_ekey_cache", {})
        slot = bucket.setdefault(
            self.config_entry.entry_id,
            {"fetched_at": None, "summaries": [], "lock": asyncio.Lock()},
        )
        async with slot["lock"]:
            if slot["fetched_at"] and slot["fetched_at"] > now - self._slow_interval:
                return slot["summaries"]
            slot["fetched_at"] = now
            summaries = await self.api.get_locks()
            await async_store_enumeration(
                self.hass,
                self._store,
                self.config_entry.entry_id,
                summaries,
            )
            if summaries:
                slot["summaries"] = summaries
                self._reload_if_locks_changed(summaries)
            return slot["summaries"]

    @callback
    def _reload_if_locks_changed(self, summaries: list[LockSummary]) -> None:
        """Rebuild the entry when the account gained or lost a lock."""
        assert self.config_entry is not None
        entry_id = self.config_entry.entry_id
        loaded = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if loaded is None:
            return
        known = {coordinator.lock_id for coordinator in loaded[TT_LOCKS]}
        if {summary.id for summary in summaries} != known:
            self.hass.config_entries.async_schedule_reload(entry_id)

    @callback
    def async_start_webhook_tracking(self) -> CALLBACK_TYPE:
        """Begin handling the webhook events webhook.py dispatches.

        Returns an unsubscribe callable for the caller to register with
        `entry.async_on_unload`. Without it a reload leaves the replaced
        coordinator subscribed, so every later event is handled once per
        reload the entry has seen.
        """
        return async_dispatcher_connect(
            self.hass, SIGNAL_NEW_DATA, self._process_webhook_data
        )

    @callback
    def async_start_ble_tracking(self) -> CALLBACK_TYPE:
        """Begin watching this lock's Bluetooth presence.

        Returns an unsubscribe callable for the caller to register with
        `entry.async_on_unload`. When Bluetooth is off, or the bluetooth
        component isn't set up, there is nothing to watch, so this is a
        no-op that returns a no-op - the lock keeps working over the cloud
        exactly as before.
        """
        if not self.ble_enabled:
            return lambda: None
        return async_track_address(self.hass, self.data.mac, self._async_ble_updated)

    @callback
    def async_start_local_refresh(self) -> CALLBACK_TYPE:
        """Re-read state over the radio between polls, for locks that need it.

        Registered for every lock; whether a tick does anything is decided
        then, because both halves of that decision change while running.
        """
        return async_track_time_interval(
            self.hass,
            self._async_local_refresh,
            LOCAL_REFRESH_INTERVAL,
            name=f"ttlock local state refresh for {self.lock_id}",
        )

    async def _async_local_refresh(self, now: datetime) -> None:
        """Read the bolt locally and publish it if it moved.

        Only for a lock whose live path is local: one the cloud reaches already
        has the webhook, which is faster and costs the lock nothing. A failed
        read publishes nothing and doesn't count against the poll's grace -
        availability stays the poll's call. Publishes without
        `async_set_updated_data`, which would reschedule the poll and, at
        this cadence, starve the slow tier that supplies the AES key.
        """
        if self.cloud_connectable or not self.ble_reachable:
            return

        status = await self.async_read_lock_status(cloud=False)
        if status is None:
            return

        self._async_note_verified()

        new_data = deepcopy(self.data)
        new_data.locked = status.locked
        if status.battery is not None:
            new_data.battery_level = status.battery
        if new_data.sensor is not None and status.door_open is not None:
            new_data.sensor.opened = status.door_open

        if new_data == self.data:
            return
        self.data = new_data
        self.async_update_listeners()

    @property
    def ble_proven(self) -> bool:
        """Whether a local read succeeded recently enough to vouch for the lock.

        TTLock locks advertise in bursts minutes apart, so a completed read
        is better evidence of reachability than the age of the last packet.
        """
        if self._ble_verified_at is None:
            return False
        return dt_util.utcnow() - self._ble_verified_at <= self._proven_reachable_for

    @property
    def ble_enabled(self) -> bool:
        """Whether local Bluetooth is on for this entry and the component is loaded."""
        return self._ble_option and async_bluetooth_available(self.hass)

    @property
    def ble_control_enabled(self) -> bool:
        """Whether to try driving the bolt over Bluetooth before the cloud.

        Gated on the radio being on too: controlling the lock locally is
        pointless without it, and the option has no effect on its own.
        """
        return self.ble_enabled and self._ble_control_option

    @property
    def ble_reachable(self) -> bool:
        """Whether the local radio can reach the lock right now."""
        if not self._ble_option:
            return False
        return self.ble_proven or (self.ble.in_range and self.ble.connectable)

    @property
    def connectable(self) -> bool:
        """Whether anything - cloud or radio - can reach the lock."""
        return self.cloud_connectable or self.ble_reachable

    @property
    def access_level(self) -> AccessLevel:
        """Account-to-lock relationship from the current ekey record."""
        return self.summary.access_level

    @property
    def ekey_remote_unlock(self) -> bool:
        """Whether the ekey currently allows remote (gateway) unlock."""
        return self.summary.remote_unlock_enabled

    @property
    def ekey_key_status(self) -> str | None:
        """Raw keyStatus wire value from v3/key/list, for diagnostics."""
        return self.summary.keyStatus

    @property
    def ekey_start_date(self) -> int:
        """Millisecond epoch the ekey becomes valid (0 = unbounded)."""
        return self.summary.startDate

    @property
    def ekey_end_date(self) -> int:
        """Millisecond epoch the ekey expires (0 = unbounded)."""
        return self.summary.endDate

    @property
    def gateway_online(self) -> bool:
        """Whether at least one linked gateway for this lock is online."""
        if not self.cloud_connectable or not self.has_gateway:
            return False
        if (
            self.config_entry is not None
            and DOMAIN in self.hass.data
            and self.config_entry.entry_id in self.hass.data[DOMAIN]
            and TT_GATEWAYS in self.hass.data[DOMAIN][self.config_entry.entry_id]
        ):
            gw_coord = self.hass.data[DOMAIN][self.config_entry.entry_id][TT_GATEWAYS]
            if not gw_coord.last_update_success or not gw_coord.data:
                return False
            if self.data.gateways:
                return any(
                    (gw := gw_coord.data.get(link.id)) is not None and gw.is_online
                    for link in self.data.gateways
                )
            return any(gw.is_online for gw in gw_coord.data.values())
        return False

    @callback
    def _async_ble_updated(self, ble: BleData) -> None:
        """Record new presence and let entities re-read it.

        Advertisements themselves never trigger a poll or a connection; only
        the lock becoming (un)reachable starts or stops the poll.
        """
        was_connectable = self.connectable
        self.ble = ble
        self.async_update_listeners()
        if self.connectable != was_connectable:
            self._async_reconcile_polling()

    @callback
    def _async_reconcile_polling(self) -> None:
        """Start or stop the poll to match what can reach the lock now."""
        if not self.connectable:
            self.update_interval = None
            return
        self.update_interval = self._poll_interval
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _async_resume_normal_polling(self) -> None:
        """Drop back to the normal cadence after a burst of retries."""
        if self.update_interval == RETRY_INTERVAL:
            self.update_interval = self._poll_interval

    @callback
    def _async_note_verified(self) -> None:
        self._verified_at = dt_util.utcnow()
        self._reverify_failures = 0
        self._async_resume_normal_polling()

    @callback
    def _async_tolerate_failed_verification(self) -> bool:
        """Whether to keep the last known state after a failed state read.

        Only for a lock something can still reach - the radio or an online
        gateway: one read that didn't take says nothing about the bolt. A lock
        nothing can reach goes unavailable on the first failure. Retries are
        quick only while the grace lasts - past it the lock is unavailable
        either way, and dialling it every minute just spends its battery.
        """
        if not (self.ble_reachable or self.gateway_online):
            return False
        self._reverify_failures += 1
        if self._reverify_failures > REVERIFY_GRACE:
            self._async_resume_normal_polling()
            return False
        self.update_interval = RETRY_INTERVAL
        return True

    def _ble_credentials(self) -> tuple[LockVersion, bytes] | None:
        """The protocol dialect and AES key, or None if lock/detail's aren't usable."""
        if self._ble_version is None or self._ble_key is None:
            return None
        with suppress(ValueError):
            return LockVersion.from_cloud(self._ble_version), parse_aes_key(
                self._ble_key
            )
        return None

    async def async_read_state_ble(self) -> LockStatus | None:
        """Read lock state over Bluetooth, or None if that isn't possible right now.

        Never raises - the caller tries the next transport, and a lock that is
        out of range or not answering is expected, not exceptional.
        """
        if not self.ble_enabled or not self.ble_reachable:
            return None
        credentials = self._ble_credentials()
        if credentials is None:
            return None
        version, aes_key = credentials
        try:
            async with self._local_lock:
                status = await async_read_status(
                    self.hass, self.data.mac, self.data.name, version, aes_key
                )
        except (BleError, ProtocolError) as err:
            _LOGGER.debug("Local state read for lock %s failed: %s", self.lock_id, err)
            self._ble_verified_at = None
            return None
        self._ble_verified_at = dt_util.utcnow()
        return status

    async def _persist_ble_credentials(self) -> None:
        """Cache the local credentials so control survives a cloud-less restart.

        lock/detail is the only source of these and it needs the cloud, so
        without a cache a restart while the cloud is unreachable would leave the
        lock undriveable until the cloud came back. Written to HA's .storage -
        the same trust boundary as the OAuth token already kept there - and
        never logged.
        """
        await self._store.async_update(
            self.lock_id,
            ble_version=self._ble_version,
            ble_key=self._ble_key,
            ble_unlock_key=self._ble_unlock_key,
            ble_lock_flag_pos=self._ble_lock_flag_pos,
        )

    async def async_restore_lock_cache(self) -> None:
        """Load the cached credentials, lock/detail and passage config at setup.

        Lets the lock be driven and described straight after a restart, and when
        the cloud is unreachable entirely. Later successful fetches refresh and
        re-persist them. Credentials already in memory are kept.
        """
        stored = await self._store.async_get(self.lock_id)
        if self._ble_key is None and stored.get("ble_key") is not None:
            self._ble_version = stored.get("ble_version")
            self._ble_key = stored.get("ble_key")
            self._ble_unlock_key = stored.get("ble_unlock_key")
            self._ble_lock_flag_pos = stored.get("ble_lock_flag_pos", 0)
        if detail := stored.get("detail"):
            self._apply_detail(self.data, LockDetail.model_validate(detail))
        if passage := stored.get("passage_mode_config"):
            self.data.passage_mode_config = PassageModeConfig.model_validate(passage)

    async def _persist_detail(self, **fields: Any) -> None:
        """Merge `fields` into the cached lock/detail."""
        stored = await self._store.async_get(self.lock_id)
        await self._store.async_update(
            self.lock_id, detail={**stored.get("detail", {}), **fields}
        )

    async def _persist_passage_mode_config(
        self, config: PassageModeConfig | None = None
    ) -> None:
        """Cache the passage config (the current one when not given)."""
        if (config := config or self.data.passage_mode_config) is not None:
            await self._store.async_update(
                self.lock_id,
                passage_mode_config=config.model_dump(mode="json", by_alias=True),
            )

    def _apply_detail(self, data: LockState, detail: LockDetail) -> None:
        """Copy lock/detail's non-secret fields present in `detail` into `data`.

        A field the detail does not carry is left as it was: a partial detail
        must not wipe the features or model known otherwise.
        """
        present = detail.model_fields_set
        if "model" in present:
            data.model = detail.model
        if "featureValue" in present:
            data.features = Features.from_feature_value(detail.featureValue)
        if "hardwareRevision" in present:
            data.hardware_version = detail.hardwareRevision
        if "firmwareRevision" in present:
            data.firmware_version = detail.firmwareRevision
        if "autoLockTime" in present:
            data.auto_lock_seconds = detail.autoLockTime
        # An on/off the detail reports as unknown is not carried either.
        if detail.lockSound is not OnOff.unknown:
            data.lock_sound = bool(detail.lockSound)

    def _take_credentials(self, details: Lock) -> None:
        """Adopt lock/detail's credentials unless they would degrade a full set.

        A detail missing any of them (eg. a cloud hiccup or a revoked field)
        must not overwrite the last complete set, which may be the only way
        left to drive the lock.
        """
        incoming = (
            details.lockVersion,
            details.aesKeyStr,
            details.lockKey,
        )
        cached = (
            self._ble_version,
            self._ble_key,
            self._ble_unlock_key,
        )
        if all(cached) and not all(incoming):
            _LOGGER.debug(
                "Keeping the cached credentials of lock %s over an incomplete "
                "lock/detail",
                self.lock_id,
            )
            return
        self._ble_version = details.lockVersion
        self._ble_key = details.aesKeyStr
        self._ble_unlock_key = details.lockKey
        self._ble_lock_flag_pos = details.lockFlagPos

    def _ble_control_credentials(
        self,
    ) -> tuple[LockVersion, bytes, str, int, int] | None:
        """Everything a local lock/unlock needs, or None if any of it is missing.

        On top of the read credentials this adds the decoded unlockKey, the
        account uid and the key's lockFlagPos. Returns None rather than a
        partial tuple so a caller can only ever send a complete, valid command.
        """
        read = self._ble_credentials()
        if read is None or self._ble_unlock_key is None:
            return None
        version, aes_key = read
        with suppress(ValueError):
            unlock_key = decode_password(self._ble_unlock_key)
            return version, aes_key, unlock_key, self.api.uid, self._ble_lock_flag_pos
        return None

    async def async_control_ble(self, *, unlock: bool) -> bool:
        """Lock/unlock over Bluetooth; True once the lock has acted.

        Raises on a local failure (out of range, no answer, or the lock
        declining), which the drive logs before trying the next transport. A
        clean success refreshes the reachability stamp, same as a local read.
        """
        if not self.ble_control_enabled or not self.ble_reachable:
            return False
        credentials = self._ble_control_credentials()
        if credentials is None:
            return False
        version, aes_key, unlock_key, uid, lock_flag_pos = credentials
        async with self._local_lock:
            await async_control(
                self.hass,
                self.data.mac,
                self.data.name,
                version,
                aes_key,
                unlock_key,
                uid,
                lock_flag_pos,
                unlock=unlock,
            )
        self._ble_verified_at = dt_util.utcnow()
        return True

    def _log_transport_failure(
        self, what: str, transport: str, err: Exception, level: int = logging.WARNING
    ) -> None:
        _LOGGER.log(
            level,
            "%s of lock %s over %s failed (%s); trying the next transport",
            what,
            self.lock_id,
            transport,
            type(err).__name__,
        )

    @property
    def transport_order(self) -> list[str]:
        """The configured order transports are tried in."""
        return list(self._transport_order)

    @property
    def last_transport(self) -> str | None:
        """The transport that last successfully drove the bolt, or None."""
        return self._last_transport

    def _transport_reachable(self, transport: str) -> bool:
        """Whether this transport can currently reach the lock FOR US.

        Reachability, not control: BLE needs only the radio in range (a read
        path), where a lock/unlock would additionally need ble_control_enabled.
        """
        if transport == TRANSPORT_BLE:
            return self.ble_reachable
        if transport == TRANSPORT_CLOUD:
            return self.cloud_connectable
        return False

    def _control_transport_available(self, transport: str) -> bool:
        """Whether a lock/unlock right now could actually go over this transport."""
        if transport == TRANSPORT_BLE:
            return self.ble_control_enabled and self.ble_reachable
        if transport == TRANSPORT_CLOUD:
            return True
        return False

    @property
    def available_transports(self) -> list[str]:
        """Transports that can currently reach the lock for us, in configured order."""
        return [t for t in self._transport_order if self._transport_reachable(t)]

    @property
    def active_transport(self) -> str | None:
        """The preferred transport currently able to reach the lock, or None."""
        available = self.available_transports
        return available[0] if available else None

    @property
    def preferred_control_transport(self) -> str | None:
        """The transport a lock/unlock would genuinely use right now, or None."""
        for transport in self._transport_order:
            if self._control_transport_available(transport):
                return transport
        return None

    @property
    def control_cloud_free(self) -> bool:
        """Whether a lock/unlock right now would avoid the cloud entirely."""
        return self.preferred_control_transport == TRANSPORT_BLE

    @property
    def cloud_online(self) -> bool:
        """Whether the cloud transport can currently reach this lock.

        The lock must be cloud-reachable; for a gatewayed lock that further
        means at least one linked gateway is online, where a WiFi-capable lock
        is reachable by the cloud by design.
        """
        if not self.cloud_connectable:
            return False
        if self.has_gateway:
            return self.gateway_online
        return True

    @contextmanager
    def _slow_tier_fetch(self, what: str) -> Generator[None]:
        """Run one slow-tier cloud fetch; a failure logs and keeps the old values."""
        try:
            yield
        except Exception as err:  # noqa: BLE001 - never fail the poll over detail
            _LOGGER.info(
                "Could not refresh the %s of lock %s (%s); keeping the previous values",
                what,
                self.lock_id,
                type(err).__name__,
            )

    async def _async_update_data(self) -> LockState:
        if not self.connectable:
            raise UpdateFailed(
                f"Lock {self.lock_id} has no gateway, WiFi or Bluetooth route"
            )

        now = dt_util.now()
        # Fast tier first: the state read is the poll's reason to exist and the
        # only thing that decides availability.
        status = await self._async_poll_state()
        new_data = deepcopy(self.data)
        await self._async_refresh_slow_tier(new_data, now)
        if Features.door_sensor in new_data.features:
            # a placeholder for the door state if the lock supports one
            if new_data.sensor is None:
                new_data.sensor = SensorData()
            with self._slow_tier_fetch("door sensor"):
                if sensor_present(new_data.sensor):
                    await self._update_present_sensor(new_data.sensor)
                else:
                    await self._check_for_sensor(new_data.sensor)
        else:
            new_data.sensor = None
        if status is not None:
            new_data.locked = status.locked
            if status.battery is not None:
                new_data.battery_level = status.battery
            if new_data.sensor is not None and status.door_open is not None:
                new_data.sensor.opened = status.door_open
        return new_data

    async def _async_poll_state(self) -> LockStatus | None:
        """Read the bolt for the poll, or decide what a failed read means.

        None keeps the last known state: the lock has never been read (it shows
        unknown rather than failing setup), or a lock still reachable is riding
        out its grace. Past that, the poll fails and the lock goes unavailable,
        so a stale state is never shown as fresh.
        """
        try:
            status = await self.async_read_lock_status(cloud=True)
        except Exception:
            _LOGGER.debug("State read for lock %s failed", self.lock_id, exc_info=True)
            status = None
        if status is not None:
            self._async_note_verified()
            return status
        if self._verified_at is None:
            _LOGGER.debug(
                "No state read for lock %s yet; leaving it unknown", self.lock_id
            )
        elif self._async_tolerate_failed_verification():
            _LOGGER.debug(
                "Keeping the last known state of lock %s after %d failed read(s)",
                self.lock_id,
                self._reverify_failures,
            )
        else:
            raise UpdateFailed(f"Failed to re-verify lock state for {self.lock_id}")
        return None

    async def async_read_lock_status(self, *, cloud: bool) -> LockStatus | None:
        """Read the bolt over BLE, then the cloud.

        The cloud is asked last, and only if `cloud`. None when BLE did not
        answer and the cloud wasn't asked. A cloud read that fails
        raises; the cloud's "could not read" (`2`) is a read whose bolt is None
        - a bolt we could not read is not an unlocked one.
        """
        status = await self.async_read_state_ble()
        if status is not None and status.locked is not None:
            return status
        if not cloud:
            return None
        state = await self.api.get_lock_state(self.lock_id)
        return LockStatus(
            battery=state.battery_level,
            locked=(
                state.locked == State.locked
                if state.locked in (State.locked, State.unlocked)
                else None
            ),
            door_open=(
                state.opened == SensorState.opened
                if state.opened in (SensorState.opened, SensorState.closed)
                else None
            ),
        )

    async def _async_refresh_slow_tier(
        self, new_data: LockState, now: datetime
    ) -> None:
        """Refresh the rarely-changing cloud detail that is due.

        Each fetch is stamped on attempt and a failure only logs, keeping the
        previous values: none of this decides whether the lock can be driven.
        """
        # The ekey record first - later branches read can_admin.
        with self._slow_tier_fetch("ekey"):
            await self._maybe_refresh_summary(now)

        # Skipped for MEMBER - lock/detail returns 20002 there and the summary
        # already supplies name, features and lockVersion.
        if self.access_level.can_admin and self._slow_tier_due(
            self._details_last_fetched, now
        ):
            self._details_last_fetched = now
            with self._slow_tier_fetch("lock/detail"):
                details = await self.api.get_lock(self.lock_id)
                new_data.name = details.name
                new_data.mac = details.mac
                new_data.battery_level = details.battery_level
                self._apply_detail(new_data, details)
                self._take_credentials(details)
                await self._persist_ble_credentials()
                await self._persist_detail(
                    **details.model_dump(
                        mode="json",
                        by_alias=True,
                        include=set(LockDetail.model_fields),
                        exclude_unset=True,
                    )
                )

        if self._slow_tier_due(self._passage_last_fetched, now):
            self._passage_last_fetched = now
            with self._slow_tier_fetch("passage mode config"):
                passage = await self.api.get_lock_passage_mode_config(self.lock_id)
                new_data.passage_mode_config = passage
                await self._persist_passage_mode_config(passage)

        # Gateway link (RSSI/name/mac for the signal sensor).
        if not self.has_gateway:
            new_data.gateways = []
        elif self._slow_tier_due(self._gateways_last_fetched, now):
            self._gateways_last_fetched = now
            with self._slow_tier_fetch("gateway list"):
                new_data.gateways = await self.api.get_gateways_for_lock(self.lock_id)

    def _slow_tier_due(self, last_fetched: datetime | None, now: datetime) -> bool:
        """Whether a slow-tier fetch is due, given when it last ran.

        Due if never fetched (None - e.g. first poll after a restart) or if
        the slow interval has elapsed since the last fetch.
        """
        return last_fetched is None or last_fetched <= now - self._slow_interval

    async def _refresh_sensor(self, sensor: SensorData) -> Sensor | None:
        """Query the door sensor endpoint and stamp when we last did so."""
        result = await self.api.get_sensor(self.lock_id)
        sensor.last_fetched = dt_util.now()
        return result

    async def _update_present_sensor(self, sensor: SensorData) -> None:
        """A sensor is already known-present; refresh its battery once a day."""
        if (
            sensor.last_fetched is not None
            and sensor.last_fetched >= dt_util.now() - timedelta(days=1)
        ):
            return

        result = await self._refresh_sensor(sensor)
        if result:
            sensor.battery = result.battery_level

    async def _check_for_sensor(self, sensor: SensorData) -> None:
        """No sensor confirmed present yet - probe for one.

        TTLock's API can't distinguish "no sensor paired" from a transient
        failure (see api.get_sensor), so we only give up after
        SENSOR_ABSENT_AFTER_FAILURES consecutive failures, persisted via
        self._store so the count - and the final verdict - survive HA
        restarts. Once given up, re-probe just once per restart
        (self._sensor_recheck_done) rather than resuming the failure count
        from scratch, which would poll forever on a setup that restarts HA
        often.
        """
        stored = await self._store.async_get(self.lock_id)
        confirmed_absent = stored.get("door_sensor_confirmed_absent", False)

        if confirmed_absent:
            if self._sensor_recheck_done:
                return
            self._sensor_recheck_done = True
        else:
            last_failed = stored.get("door_sensor_last_failed_req")
            last_failed_dt = (
                dt_util.parse_datetime(last_failed) if last_failed else None
            )
            if (
                last_failed_dt is not None
                and last_failed_dt >= dt_util.now() - timedelta(days=1)
            ):
                return

        result = await self._refresh_sensor(sensor)

        if result:
            sensor.battery = result.battery_level
            await self._store.async_update(
                self.lock_id,
                door_sensor_confirmed_absent=False,
                door_sensor_count_failed_req=0,
            )
            return

        if confirmed_absent:
            # this was just this restart's one-shot recheck, and it also
            # failed - leave the persisted verdict as-is rather than
            # growing the failure count forever across restarts
            return

        failures = stored.get("door_sensor_count_failed_req", 0) + 1
        newly_confirmed_absent = failures >= SENSOR_ABSENT_AFTER_FAILURES
        if newly_confirmed_absent:
            # this call is what crossed the threshold, so it also counts as
            # this instance's one-shot recheck - otherwise the very next
            # scheduled refresh (15 minutes later, not gated by the daily
            # throttle above) would see confirmed_absent for "the first
            # time" and fire one more unbudgeted check before going quiet.
            self._sensor_recheck_done = True
        await self._store.async_update(
            self.lock_id,
            door_sensor_last_failed_req=dt_util.now().isoformat(),
            door_sensor_count_failed_req=failures,
            door_sensor_confirmed_absent=newly_confirmed_absent,
        )

    @callback
    def _async_refresh_finished(self) -> None:
        """Push refreshed device info back into the device registry.

        Entities snapshot `coordinator.device_info` once at add time, so once
        we start seeding placeholder data and filling it in progressively,
        this is what keeps the device registry's model/sw_version/hw_version
        in sync with later successful refreshes.
        """
        if self.last_update_success and self.config_entry is not None:
            registry = dr.async_get(self.hass)
            registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id, **self.device_info
            )

    @callback
    def _process_webhook_data(self, event: WebhookEvent):
        """Update data."""
        if event.id != self.lock_id:
            return

        _LOGGER.debug("Lock %s received %s", self.unique_id, event)

        if not event.success:
            return

        if not self.data:
            return

        new_data = deepcopy(self.data)
        new_data.battery_level = event.battery_level

        if state := event.state:
            if state.locked == State.locked:
                new_data.locked = True
                # the countdown is over - an old timer must not outlive a lock
                self._cancel_auto_lock()
            elif state.locked == State.unlocked:
                new_data.locked = False
                self._handle_auto_lock(event.lock_ts)

            if state.locked is not None:
                self._async_note_verified()
                new_data.last_user = event.user
                new_data.last_reason = event.event.description

        if door_state_known(new_data.sensor) and event.sensorState:
            if event.sensorState.opened == SensorState.opened:
                new_data.sensor.opened = True
            if event.sensorState.opened == SensorState.closed:
                new_data.sensor.opened = False
                # A closed door says nothing about the bolt: keep the last
                # known state. A pending auto-lock timer still covers the
                # unlocked case, and the next poll or lock event corrects it.
        self.async_set_updated_data(new_data)

    @callback
    def _cancel_auto_lock(self) -> None:
        if self._auto_lock_task is not None:
            self._auto_lock_task.cancel()
            self._auto_lock_task = None

    async def async_shutdown(self) -> None:
        """Cancel any pending auto-lock timer along with scheduled refreshes."""
        self._cancel_auto_lock()
        await super().async_shutdown()

    def _handle_auto_lock(self, lock_ts: datetime):
        """Handle auto-locking the lock."""

        # a newer unlock restarts the lock's auto-lock countdown
        self._cancel_auto_lock()

        auto_lock_delay = self.data.auto_lock_delay(lock_ts)

        if auto_lock_delay is None:
            _LOGGER.debug("Auto-lock is disabled")

            return

        # Prefer our clock over the locks. Be late rather than early in checking
        async def _auto_locked(seconds: int):
            await asyncio.sleep(seconds)
            await self._async_verify_auto_lock()

        self._auto_lock_task = self.hass.async_create_task(
            _auto_locked(auto_lock_delay), eager_start=False
        )

    async def _async_verify_auto_lock(self) -> None:
        """Read the bolt once auto-lock should have run, and publish what it says.

        The door may be held open or the bolt may have jammed.
        """
        if not self.connectable:
            return

        try:
            status = await self.async_read_lock_status(cloud=True)
        except Exception:
            _LOGGER.debug(
                "Could not check lock %s after its auto-lock delay",
                self.lock_id,
                exc_info=True,
            )
            return
        if status is None or status.locked is None:
            return
        locked = status.locked
        battery = status.battery
        door_open = status.door_open

        self._async_note_verified()
        _LOGGER.debug(
            "Lock %s reads %s after its auto-lock delay",
            self.lock_id,
            "locked" if locked else "unlocked",
        )

        new_data = deepcopy(self.data)
        if locked and not new_data.locked:
            new_data.last_reason = "Auto Lock"
        new_data.locked = locked
        if battery is not None:
            new_data.battery_level = battery
        if new_data.sensor is not None and door_open is not None:
            new_data.sensor.opened = door_open

        if new_data == self.data:
            return
        self.data = new_data
        self.async_update_listeners()

    @property
    def unique_id(self) -> str:
        """Unique ID prefix for all entities for the lock."""
        return f"{DOMAIN}-{self.lock_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Device info for the lock.

        via_device_id links to the best-RSSI gateway currently in range (see
        GatewayLink), matching the identifier GatewaySensor registers in
        binary_sensor.py - this is what makes the lock's device page show
        "via device: <gateway>". Re-pushed on every successful poll by
        _async_refresh_finished, so it tracks the gateway TTLock's cloud
        would actually use if the best one changes.
        """
        info = DeviceInfo(
            identifiers={(DOMAIN, self.data.mac)},
            manufacturer="TT Lock",
            model=self.data.model,
            name=self.data.name,
            sw_version=self.data.firmware_version,
            hw_version=self.data.hardware_version,
        )
        if self.config_entry is not None and (best_gateway := self.data.best_gateway):
            gateway = dr.async_get(self.hass).async_get_device_by_identifier(
                (DOMAIN, best_gateway.mac), self.config_entry.entry_id
            )
            if gateway is not None:
                info["via_device_id"] = gateway.id
        return info

    @property
    def entities(self) -> list[Entity]:
        """Entities belonging to this co-ordinator.

        Not every registered listener is a bound entity method -
        async_add_when_sensor_present registers a plain closure while it
        waits to confirm door-sensor presence, which has no `__self__` at
        all, so that has to be tolerated rather than assumed away.
        """
        result: list[Entity] = []
        for listener_callback, _ in list(self._listeners.values()):
            owner = getattr(listener_callback, "__self__", None)
            if isinstance(owner, Entity):
                result.append(owner)
        return result

    def as_dict(self) -> dict:
        """Serialize for diagnostics."""
        return {
            "unique_id": self.unique_id,
            "connectable": self.connectable,
            "cloud_connectable": self.cloud_connectable,
            "ble_enabled": self.ble_enabled,
            "ble_control_enabled": self.ble_control_enabled,
            "ble_reachable": self.ble_reachable,
            "ble_proven": self.ble_proven,
            "transport_order": self._transport_order,
            "active_transport": self.active_transport,
            "last_transport": self.last_transport,
            "control_cloud_free": self.control_cloud_free,
            "cloud_online": self.cloud_online,
            "has_gateway": self.has_gateway,
            "feature_value": self.feature_value,
            # Ekey view of this lock from the authenticated account's
            # perspective - access_level drives capability gating, the
            # other fields explain why a service action was refused. The
            # ekey's `remarks` and `lockData` are deliberately NOT exposed
            # (set by the sharing admin / contain credentials).
            "access_level": self.access_level.value,
            "ekey_key_status": self.ekey_key_status,
            "ekey_remote_unlock": self.ekey_remote_unlock,
            "ekey_start_date": self.ekey_start_date,
            "ekey_end_date": self.ekey_end_date,
            "last_update_success": self.last_update_success,
            "device": self.data,
            "entities": [
                state.as_dict()
                for entity in self.entities
                if (state := self.hass.states.get(entity.entity_id)) is not None
            ],
            "debug_capture": self._capture.diagnostics_for(self.lock_id),
        }

    async def lock(self) -> None:
        """Lock the lock over the first available transport, in the set order."""
        await self._async_drive(unlock=False)

    async def unlock(self) -> None:
        """Unlock the lock over the first available transport, in the set order."""
        await self._async_drive(unlock=True)

    async def _async_drive(self, *, unlock: bool) -> None:
        """Try each configured, available transport in order; first success wins.

        Availability is decided per transport right now - BLE (control enabled
        and reachable) and cloud (always). A transport that fails, by declining
        or by raising, is logged and the next is tried. If everything tried
        failed, it raises so an automation never takes a failed lock or unlock
        for a done one.
        """
        action = "unlock" if unlock else "lock"
        with lock_action(self):
            tried: list[str] = []
            for transport in self._transport_order:
                attempt = self._transport_attempt(transport, unlock=unlock)
                if attempt is None:
                    continue
                tried.append(transport)
                try:
                    acted = await attempt()
                except Exception as err:  # noqa: BLE001 - next transport
                    self._log_transport_failure(action, transport, err)
                    continue
                if acted:
                    self.data.locked = not unlock
                    self._last_transport = transport
                    if unlock and transport != TRANSPORT_CLOUD:
                        # the cloud's webhook reports its own unlocks
                        self._handle_auto_lock(dt_util.now())
                    return
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"{action}_failed",
                translation_placeholders={
                    "name": self.data.name,
                    "transports": ", ".join(tried),
                },
            )

    def _transport_attempt(
        self, transport: str, *, unlock: bool
    ) -> Callable[[], Any] | None:
        """A coroutine-returning callable for an available transport, else None."""
        if transport == TRANSPORT_BLE:
            if not (self.ble_control_enabled and self.ble_reachable):
                return None
            return lambda: self.async_control_ble(unlock=unlock)
        if transport == TRANSPORT_CLOUD:
            return lambda: self._async_control_cloud(unlock=unlock)
        return None

    async def _async_control_cloud(self, *, unlock: bool) -> bool:
        """Drive the bolt over the TTLock cloud; False if the cloud declined."""
        if unlock:
            return bool(await self.api.unlock(self.lock_id))
        return bool(await self.api.lock(self.lock_id))

    async def set_auto_lock(self, on: bool) -> None:
        """Turn on/off Autolock."""
        seconds = 10 if on else 0
        res = await self.api.set_auto_lock(self.lock_id, seconds)
        if res:
            self.data.auto_lock_seconds = seconds
            if not on:
                # a countdown started under the old setting no longer applies
                self._cancel_auto_lock()
            self.async_update_listeners()

    async def set_lock_sound(self, on: bool) -> None:
        """Turn on/off lock sound."""
        value = 1 if on else 2
        res = await self.api.set_lock_sound(self.lock_id, value)
        if res:
            self.data.lock_sound = on
            self.async_update_listeners()


class GatewaysUpdateCoordinator(DataUpdateCoordinator[dict[int, Gateway]]):
    """Class to manage fetching Gateway data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: TTLockApi,
        store: LockStateStore,
    ) -> None:
        """Initialize the update co-ordinator for gateways."""
        self.api = api
        self._store = store

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-gateways",
            config_entry=config_entry,
            update_interval=timedelta(minutes=15),
        )

    @callback
    def async_restore(self, records: list[dict[str, Any]]) -> None:
        """Seed the last persisted gateway list, used until a refresh lands."""
        self.data = {
            gateway.id: gateway
            for gateway in (Gateway.model_validate(record) for record in records)
        }

    async def _async_update_data(self) -> dict[int, Gateway]:
        assert self.config_entry is not None
        try:
            gateways = await self.api.get_gateways()
        except Exception as err:
            raise UpdateFailed(err) from err
        await self._store.async_update_entry(
            self.config_entry.entry_id,
            gateways=[
                gateway.model_dump(mode="json", by_alias=True) for gateway in gateways
            ],
        )
        return {gateway.id: gateway for gateway in gateways}

    def as_dict(self) -> list[dict]:
        """Serialize for diagnostics."""
        return [
            {
                "id": gateway.id,
                "name": gateway.name,
                "is_online": gateway.is_online,
            }
            for gateway in (self.data or {}).values()
        ]
