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
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from functools import partial
import logging
from typing import Any, TypeGuard

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import lock_commands
from .api import TTLockApi
from .ble import (
    BleData,
    BleError,
    async_admin_session,
    async_bluetooth_available,
    async_control,
    async_read_status,
    async_track_address,
)
from .ble_protocol import (
    AutoLockConfig,
    Command,
    CredentialError,
    LockStatus,
    LockVersion,
    ProtocolError,
    decode_password,
    lock_timezone,
    parse_aes_key,
)
from .capture import LockTrafficCapture
from .const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_CLOUD_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_SLOW_POLL_INTERVAL,
    CONF_WIFI_LOCAL_ENABLED,
    DEFAULT_BLE_CONTROL,
    DEFAULT_BLUETOOTH_ENABLED,
    DEFAULT_CLOUD_ENABLED,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_SLOW_POLL_INTERVAL_HOURS,
    DEFAULT_WIFI_LOCAL_ENABLED,
    DOMAIN,
    SIGNAL_NEW_DATA,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TRANSPORT_WIFI_LOCAL,
    TT_GATEWAYS,
    TT_LOCKS,
    TT_WIFI_PROXY,
    transport_order,
)
from .lock_commands import CommandRejected
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
from .wifi_proxy import (
    MODE_PASSTHROUGH,
    MODE_TAKEOVER,
    WifiCredentials,
    WifiProxy,
)

_LOGGER = logging.getLogger(__name__)

# The wifi-local link states surfaced by the connection diagnostics. The two
# live-session values reuse wifi_proxy's own mode strings so the sensor never
# drifts from what session_mode() returns; the other two cover "no session but
# reconnecting" and "nothing".
WIFI_LOCAL_DOWN = "down"
WIFI_LOCAL_RECONNECTING = "reconnecting"
WIFI_LOCAL_STATES = (
    WIFI_LOCAL_DOWN,
    MODE_PASSTHROUGH,
    MODE_TAKEOVER,
    WIFI_LOCAL_RECONNECTING,
)

NOT_CONNECTABLE_REASON = (
    "No gateway paired, no WiFi, and not in Bluetooth range of Home Assistant - "
    "nothing can reach this lock. Live data isn't available until one of those "
    "changes."
)


class NoLocalAdminTransport(HomeAssistantError):
    """No local transport (BLE or WiFi-local) could run an admin feature op.

    Either none was reachable/credentialled, or every one that was reachable
    failed before the op was sent. The caller falls back to the cloud where
    there is a cloud equivalent; otherwise it reaches the user as is.
    """

    def __init__(self, name: str) -> None:
        """Name the lock in the translated message."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="no_local_transport",
            translation_placeholders={"name": name},
        )


class LocalOutcomeUnknown(HomeAssistantError):
    """A non-idempotent op was sent and then lost its answer.

    The lock may or may not have applied it, so it is never re-run over another
    transport (that could add a credential twice) and never falls back to the
    cloud: deliberately not a NoLocalAdminTransport.
    """


class LocalReadInterrupted(Exception):
    """A lock/unlock wanted the Bluetooth link a long local read was holding.

    The read gives the link up at its next command; being idempotent, it is
    simply tried over the next transport.
    """


class LocalReadYielded(Exception):
    """A local op is waiting for the admin op a read is running.

    The read stops at its next command and runs again after that op.
    """


class _InterruptibleReadSession:
    """An admin session for a read, given up at its next command when needed.

    A waiting write stops the read on any transport, and a background read
    also stops for a waiting read. A waiting lock/unlock stops it only on
    Bluetooth, where the read holds the link.
    """

    def __init__(
        self,
        session: lock_commands.AdminSession,
        coordinator: LockUpdateCoordinator,
        *,
        ble: bool,
        background: bool,
    ) -> None:
        """Wrap `session`; `coordinator` says what is waiting."""
        self._session = session
        self._coordinator = coordinator
        self._ble = ble
        self._background = background

    async def command(self, opcode: Command, data: bytes = b"") -> bytes:
        """Send one command, unless an op it yields to is waiting."""
        waiting = self._coordinator._ops_waiting  # noqa: SLF001
        if waiting["write"] or (self._background and waiting["read"]):
            raise LocalReadYielded
        if self._ble and self._coordinator._controls_waiting:  # noqa: SLF001
            raise LocalReadInterrupted
        return await self._session.command(opcode, data)


# A read of the lock's operation log returns within this many seconds. It starts
# no page in the last LOG_READ_MARGIN of them, so it normally ends between
# commands; a command still running at the limit is cancelled.
LOG_READ_TIME_LIMIT = 45.0
LOG_READ_MARGIN = 10.0
# A lock event on the WiFi session reads the log this many seconds later, once
# for a burst of events, so the record is written first.
LOG_EVENT_DELAY = 5.0

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


def _on_off(on: bool) -> int:
    """A bool as lock/detail's wire on/off value."""
    return (OnOff.on if on else OnOff.off).value


async def _read_settings(
    session: lock_commands.AdminSession, read: dict[str, Any], features: Features
) -> None:
    """Read the lock's saved settings into `read`, as lock/detail fields.

    Each value lands in `read` as soon as it is decoded, so a later read that
    fails keeps the earlier ones. Sound goes last: its read is the least proven.
    A switch the lock lacks the feature for is not recorded.
    """
    if (switches := await lock_commands.get_switch_config(session)) is not None:
        if Features.tamper_alert in features:
            read["tamperAlert"] = _on_off(switches.settings["tamper_alert"])
        if Features.passage_mode in features:
            read["passageModeAutoUnlock"] = _on_off(
                switches.settings["passage_mode_auto_unlock"]
            )
    if (seconds := (await lock_commands.get_auto_lock(session)).seconds) is not None:
        read["autoLockTime"] = seconds
    if (sound := await lock_commands.get_lock_sound(session)) is not None:
        read["lockSound"] = _on_off(sound)


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


def _wifi_mac_from_ble(ble_mac: str) -> str | None:
    """Derive the lock's WiFi client MAC from its BLE MAC.

    A heuristic: on the WiFi locks seen so far the WiFi MAC is the BLE MAC with
    the last octet incremented by one. Returns None for a missing/malformed MAC,
    or when the last octet is 0xFF (wrapping to 00 could point at an unrelated
    client), so a bad value just omits the WiFi connection rather than crashing
    device_info.
    """
    if not ble_mac:
        return None
    octets = ble_mac.split(":")
    if len(octets) != 6:
        return None
    try:
        values = [int(octet, 16) for octet in octets]
    except ValueError:
        return None
    if values[-1] == 0xFF:
        return None
    values[-1] += 1
    return ":".join(f"{value:02X}" for value in values)


def _validity_or_none(value: datetime | None) -> datetime | None:
    """Treat an epoch-0 timestamp as "no bound" (permanent).

    The passcode endpoint uses startDate/endDate = 0 to mean permanent, but the
    Passcode model (unlike Card/Fingerprint) has no zero->None mapping, so a 0
    parses to 1970-01-01. Normalise it here so a permanent passcode shows no
    validity window instead of a 1970 one. Leaving the model alone keeps the
    service actions that consume it unchanged.
    """
    if value is None:
        return None
    return None if value.timestamp() == 0 else value


@dataclass
class CredentialInfo:
    """A credential's human-facing metadata - never its secret digits.

    Built from the cloud list endpoints so local credential management can show
    real names instead of opaque ids. The passcode/card/fingerprint NUMBER is
    deliberately absent: only id -> name/type/validity is ever stored.
    """

    name: str | None = None
    type: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


@dataclass
class CredentialNames:
    """Per-kind id -> CredentialInfo maps for one lock."""

    passcodes: dict[int, CredentialInfo] = field(default_factory=dict)
    cards: dict[int, CredentialInfo] = field(default_factory=dict)
    fingerprints: dict[int, CredentialInfo] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        """How many of each kind are known."""
        return {
            "passcodes": len(self.passcodes),
            "cards": len(self.cards),
            "fingerprints": len(self.fingerprints),
        }


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
    tamper_alert: bool | None = None
    passage_auto_unlock: bool | None = None
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
) -> GatewaysUpdateCoordinator | None:
    """Get the gateway coordinator; None while the entry runs with the cloud off."""
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
        self._timezone_raw_offset = summary.timezoneRawOffset
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
        # The admin password (cloud `adminPwd`, obfuscated), needed by the
        # wifi_local CHECK_ADMIN handshake - which the BLE control path never
        # used (it goes via CHECK_USER_TIME). Decoded on use, like the AES key.
        self._ble_admin_pwd: str | None = None

        # Whether we've already made this restart's one-shot door-sensor
        # recheck (see _check_for_sensor) - deliberately in-memory, not
        # persisted, so it naturally resets to False on every restart.
        self._sensor_recheck_done = False

        # The pending "assume auto-locked" timer started by the last unlock, if
        # any - kept so a newer unlock (or unload) can cancel it.
        self._auto_lock_task: asyncio.Task | None = None

        # The Bluetooth link: one GATT conversation with the lock at a time. A
        # write holds it for its whole admin op on any transport (a 0x68 switch
        # write is a read-modify-write that two concurrent writes would lose one
        # of); a read holds it only while it has a Bluetooth session open, and
        # gives it up at its next command when a lock/unlock is waiting.
        self._local_lock = asyncio.Lock()
        # One admin op at a time, on any transport; lock/unlock never takes it.
        # A read gives it up at its next command when a write is waiting, and
        # a background read also when a read is waiting.
        self._admin_lock = asyncio.Lock()
        self._controls_waiting = 0
        self._ops_waiting: Counter[str] = Counter()
        # The operation log read from the lock so far, loaded from the store on
        # first use and changed only by an admin op.
        self._operate_log: lock_commands.OperateLog | None = None
        # Log reads running, and whether a lock event asked for another.
        self._log_reads_running = 0
        self._log_read_again = False

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
        self._transport_order: list[str] = transport_order(options)
        self._cloud_enabled_option: bool = options.get(
            CONF_CLOUD_ENABLED, DEFAULT_CLOUD_ENABLED
        )
        self._wifi_local_option: bool = options.get(
            CONF_WIFI_LOCAL_ENABLED, DEFAULT_WIFI_LOCAL_ENABLED
        )
        self._details_last_fetched: datetime | None = None
        # Whether the last lock/detail fetch landed; while it has, the settings
        # are the cloud's and are not also read from the lock.
        self._cloud_detail_ok = False
        # When the settings were last read from the lock (see
        # _async_refresh_settings_locally).
        self._local_settings_last_read: datetime | None = None
        self._passage_last_fetched: datetime | None = None
        self._gateways_last_fetched: datetime | None = None

        # The transport that last successfully drove the bolt (BLE / wifi_local /
        # cloud), surfaced by the connection diagnostics. None until the first
        # lock/unlock lands.
        self._last_transport: str | None = None

        # Cloud-sourced id -> name/type/validity for the lock's passcodes, IC
        # cards and fingerprints, so local credential management shows real
        # names. Refreshed opportunistically on the slow tier and persisted
        # (names only, never secret digits). Restored lazily when still empty.
        self.credential_names: CredentialNames = CredentialNames()
        self._credentials_last_fetched: datetime | None = None
        self._credentials_restored = False

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
        self._log_event_read = Debouncer(
            hass,
            _LOGGER,
            cooldown=LOG_EVENT_DELAY,
            immediate=False,
            function=self._async_background_log_read,
            background=True,
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
        """Read the bolt locally and publish it if it moved, then read the log.

        Only for a lock whose live path is local: one the cloud reaches already
        has the webhook, which is faster and costs the lock nothing. A failed
        read publishes nothing and doesn't count against the poll's grace -
        availability stays the poll's call. Publishes without
        `async_set_updated_data`, which would reschedule the poll and, at
        this cadence, starve the slow tier that supplies the AES key.
        """
        if (self.cloud_enabled and self.cloud_connectable) or not (
            self.ble_reachable or self.wifi_local_drivable
        ):
            return

        status = await self.async_read_lock_status(cloud=False)
        if status is not None:
            self._async_publish_local_status(status)
        await self._async_background_log_read()

    @callback
    def _async_publish_local_status(self, status: LockStatus) -> None:
        """Publish a local state read if it changed anything."""
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
        """Whether anything - cloud, radio or WiFi-local - can reach the lock."""
        return (
            (self.cloud_enabled and self.cloud_connectable)
            or self.ble_reachable
            or self.wifi_local_drivable
        )

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
            if (
                gw_coord is None
                or not gw_coord.last_update_success
                or not gw_coord.data
            ):
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

        Only for a lock something can still reach - the radio, an online
        gateway or a WiFi-local session (or one reconnecting): one read that
        didn't take says nothing about the bolt. A lock nothing can reach goes
        unavailable on the first failure. Retries are quick only while the
        grace lasts - past it the lock is unavailable either way, and dialling
        it every minute just spends its battery.
        """
        if not (self.ble_reachable or self.gateway_online or self.wifi_local_drivable):
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
            ble_admin_pwd=self._ble_admin_pwd,
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
            self._ble_admin_pwd = stored.get("ble_admin_pwd")
        if detail := stored.get("detail"):
            self._apply_detail(self.data, LockDetail.model_validate(detail))
        if passage := stored.get("passage_mode_config"):
            self.data.passage_mode_config = PassageModeConfig.model_validate(passage)

    async def _persist_detail(self, **fields: Any) -> None:
        """Update the cached lock/detail fields a local write just changed."""
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

        A field the detail does not carry is left as it was: a cache holding
        only a local write (eg. autoLockTime, before lock/detail was ever
        cached) must not wipe the features, model or timezone known otherwise.
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
        if detail.tamperAlert is not OnOff.unknown:
            data.tamper_alert = bool(detail.tamperAlert)
        if detail.passageModeAutoUnlock is not OnOff.unknown:
            data.passage_auto_unlock = bool(detail.passageModeAutoUnlock)
        if "timezoneRawOffset" in present:
            self._timezone_raw_offset = detail.timezoneRawOffset

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
            details.noKeyPwd,
        )
        cached = (
            self._ble_version,
            self._ble_key,
            self._ble_unlock_key,
            self._ble_admin_pwd,
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
        self._ble_admin_pwd = details.noKeyPwd

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
        # Counted while waiting: a long read gives the link up for this.
        self._controls_waiting += 1
        waiting = True
        try:
            async with self._local_lock:
                self._controls_waiting -= 1
                waiting = False
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
        finally:
            if waiting:
                self._controls_waiting -= 1
        self._ble_verified_at = dt_util.utcnow()
        return True

    async def async_run_local_admin_op[T](
        self,
        what: str,
        op: Callable[[lock_commands.AdminSession], Awaitable[T]],
        *,
        idempotent: bool,
        read_only: bool = False,
        log_level: int = logging.WARNING,
        background: bool = False,
    ) -> T:
        """Run `op` over the first local transport that can, in configured order.

        BLE and WiFi-local are tried in `transport_order`; the cloud is never a
        local transport and is the caller's fallback. A transport that is not
        reachable or not credentialled is skipped, and one that fails before
        `op` is sent is logged and the next is tried. Once `op` has been sent a
        lost answer is `LocalOutcomeUnknown` for a non-idempotent op, never a
        re-run elsewhere. The lock saying no is `local_command_rejected` - the
        user's answer, not a reason to try elsewhere - and nothing local being
        able to carry it is `NoLocalAdminTransport`, so the caller can fall back
        to the cloud. All three are translated HomeAssistantErrors.

        Admin ops run one at a time. A write holds the Bluetooth link for the
        whole op. A `read_only` op (which must change nothing on the lock) holds
        it only while its Bluetooth session is open and gives it up at its next
        command when a lock/unlock is waiting, moving on to the next transport;
        so a lock/unlock never waits behind a long read. A read also stops at
        its next command when a write is waiting, and runs again after it. A
        `background` read goes over WiFi-local only and also stops for any
        waiting op, a read included. It never counts as waiting itself. A
        transport failure is logged at `log_level`.
        """
        link: AbstractAsyncContextManager[Any] = (
            nullcontext() if read_only else self._local_lock
        )
        while True:
            try:
                async with (
                    self._admin_turn(read_only=read_only, background=background),
                    link,
                ):
                    return await self._run_local_admin_op(
                        what,
                        op,
                        idempotent=idempotent or read_only,
                        read_only=read_only,
                        log_level=log_level,
                        background=background,
                    )
            except CommandRejected as err:
                raise self._rejected(err) from err
            except LocalReadYielded:
                continue

    @asynccontextmanager
    async def _admin_turn(
        self, *, read_only: bool, background: bool = False
    ) -> AsyncGenerator[None]:
        """Hold the admin-op queue. An op counts as waiting until its turn.

        A background read never counts, so it interrupts nothing.
        """
        if background:
            async with self._admin_lock:
                yield
            return
        kind = "read" if read_only else "write"
        self._ops_waiting[kind] += 1
        waiting = True
        try:
            async with self._admin_lock:
                self._ops_waiting[kind] -= 1
                waiting = False
                yield
        finally:
            if waiting:
                self._ops_waiting[kind] -= 1

    async def async_read_operate_log(
        self, *, background: bool = False
    ) -> lock_commands.OperateLog:
        """Read the lock's operation log on from where the last read stopped.

        The read returns within `LOG_READ_TIME_LIMIT` seconds, waiting for its
        turn included; what it read by then is kept. Returns a copy of the log
        read so far, persisted whether or not the read succeeded. A
        `background` read goes over WiFi-local only, gives way to any waiting
        op and logs failures at debug. A lock event during the read asks for
        one more background read after it.
        """
        if self._operate_log is None:
            stored = (await self._store.async_get(self.lock_id)).get("operate_log", {})
            self._operate_log = lock_commands.OperateLog(
                last_sequence=stored.get("last_sequence"),
                records=stored.get("records", []),
            )
        log = self._operate_log
        deadline = asyncio.get_running_loop().time() + LOG_READ_TIME_LIMIT
        self._log_reads_running += 1
        try:
            async with asyncio.timeout_at(deadline):
                await self.async_run_local_admin_op(
                    "read operate log",
                    partial(
                        lock_commands.read_operate_log,
                        log=log,
                        stop_at=deadline - LOG_READ_MARGIN,
                    ),
                    idempotent=True,
                    read_only=True,
                    log_level=logging.DEBUG if background else logging.WARNING,
                    background=background,
                )
        except TimeoutError:
            pass
        finally:
            self._log_reads_running -= 1
            if self._log_read_again and not self._log_reads_running:
                self._log_read_again = False
                self._log_event_read.async_schedule_call()
            snapshot = replace(log, records=list(log.records))
            await self._store.async_update(
                self.lock_id,
                operate_log={
                    "last_sequence": snapshot.last_sequence,
                    "records": snapshot.records,
                },
            )
        return snapshot

    async def _async_background_log_read(self) -> None:
        """Read the operation log on into the cache, when it can be done cheaply.

        Only with the cloud off, for an admin key, over a ready WiFi-local
        takeover session: a background Bluetooth connection costs the lock's
        battery. A failure logs at debug and keeps the cache.
        """
        if not self._log_readable_in_background:
            return
        try:
            await self.async_read_operate_log(background=True)
        except Exception as err:  # noqa: BLE001 - never fail over the log
            _LOGGER.debug(
                "Could not read the operation log of lock %s (%s)",
                self.lock_id,
                type(err).__name__,
            )

    @property
    def _log_readable_in_background(self) -> bool:
        """Cloud off, an admin key and a ready WiFi-local takeover session."""
        proxy = self._wifi_proxy
        return (
            not self.cloud_enabled
            and self.access_level.can_admin
            and proxy is not None
            and proxy.is_takeover_ready(self.lock_id)
        )

    def _rejected(self, err: CommandRejected) -> HomeAssistantError:
        """The lock's refusal as the translated error the user sees."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="local_command_rejected",
            translation_placeholders={"name": self.data.name, "reason": str(err)},
        )

    def _outcome_unknown(self) -> LocalOutcomeUnknown:
        return LocalOutcomeUnknown(
            translation_domain=DOMAIN,
            translation_key="local_outcome_unknown",
            translation_placeholders={"name": self.data.name},
        )

    async def _run_local_admin_op[T](
        self,
        what: str,
        op: Callable[[lock_commands.AdminSession], Awaitable[T]],
        *,
        idempotent: bool,
        read_only: bool,
        log_level: int,
        background: bool,
    ) -> T:
        # A read takes the Bluetooth link only for its own session; a write
        # already holds it.
        ble_link: AbstractAsyncContextManager[Any] = (
            self._local_lock if read_only else nullcontext()
        )
        for transport in self._transport_order:
            if background and transport != TRANSPORT_WIFI_LOCAL:
                continue
            # Whether a feature command got past the handshake: from then on a
            # lost answer leaves the outcome unknown.
            sent = False

            def mark_sent() -> None:
                nonlocal sent
                sent = True

            try:
                if transport == TRANSPORT_BLE:
                    if not (self.ble_control_enabled and self.ble_reachable):
                        continue
                    control = self._ble_control_credentials()
                    if control is None or (admin_ps := self.admin_ps) is None:
                        continue
                    version, aes_key, unlock_key, uid, lock_flag_pos = control
                    async with (
                        ble_link,
                        async_admin_session(
                            self.hass,
                            self.data.mac,
                            self.data.name,
                            version,
                            aes_key,
                            admin_ps,
                            unlock_key,
                            uid,
                            lock_flag_pos,
                        ) as session,
                    ):
                        mark_sent()  # the handshake is done; op sends at once
                        result = await op(
                            _InterruptibleReadSession(
                                session, self, ble=True, background=background
                            )
                            if read_only
                            else session
                        )
                    self._ble_verified_at = dt_util.utcnow()
                    return result
                if transport == TRANSPORT_WIFI_LOCAL:
                    proxy = self._wifi_proxy
                    if proxy is None or not self.wifi_local_drivable:
                        continue
                    if self.wifi_credentials() is None:
                        continue
                    attempted, wifi_result = await proxy.async_admin_op(
                        self.lock_id,
                        (
                            lambda session: op(
                                _InterruptibleReadSession(
                                    session, self, ble=False, background=background
                                )
                            )
                        )
                        if read_only
                        else op,
                        mark_sent,
                    )
                    if attempted:
                        return wifi_result
            except (CommandRejected, LocalReadYielded):
                raise
            except ValueError as err:
                if not isinstance(err, CredentialError):
                    raise  # malformed input: no transport could send it
                self._log_transport_failure(what, transport, err, log_level)
            except Exception as err:
                if sent and not idempotent:
                    raise self._outcome_unknown() from err
                self._log_transport_failure(what, transport, err, log_level)
        raise NoLocalAdminTransport(self.data.name)

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
    def admin_ps(self) -> int | None:
        """The lock's admin password as a number, or None if not available yet.

        Decoded from the cloud's obfuscated `adminPwd` (stored as the lock's
        `noKeyPwd` field). Needed by the wifi_local CHECK_ADMIN handshake.
        """
        if self._ble_admin_pwd is None:
            return None
        with suppress(ValueError):
            return int(decode_password(self._ble_admin_pwd))
        return None

    def wifi_credentials(self) -> WifiCredentials | None:
        """Everything the wifi_local command path needs, or None if incomplete."""
        control = self._ble_control_credentials()
        admin_ps = self.admin_ps
        if control is None or admin_ps is None:
            return None
        _version, aes_key, unlock_key, uid, lock_flag_pos = control
        return WifiCredentials(
            aes_key=aes_key,
            admin_ps=admin_ps,
            unlock_key=unlock_key,
            uid=uid,
            lock_flag_pos=lock_flag_pos,
        )

    @property
    def _wifi_proxy(self) -> WifiProxy | None:
        """The shared wifi_proxy listener, if one is running.

        `connectable` reads this, and it is evaluated inside `__init__` before
        `DataUpdateCoordinator.__init__` has set `self.hass`, so tolerate that
        early call (no proxy is running that early anyway).
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return None
        return hass.data.get(DOMAIN, {}).get(TT_WIFI_PROXY)

    @property
    def cloud_enabled(self) -> bool:
        """Whether the cloud transport may be used for this entry."""
        return self._cloud_enabled_option

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
        if transport == TRANSPORT_WIFI_LOCAL:
            return self.wifi_local_drivable
        # The cloud: `transport_order` admits no other name.
        return self.cloud_enabled and self.cloud_connectable

    def _control_transport_available(self, transport: str) -> bool:
        """Whether a lock/unlock right now could actually go over this transport."""
        if transport == TRANSPORT_BLE:
            return self.ble_control_enabled and self.ble_reachable
        if transport == TRANSPORT_WIFI_LOCAL:
            return self.wifi_local_drivable
        # The cloud: `transport_order` admits no other name.
        return self.cloud_enabled

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
        return self.preferred_control_transport in (
            TRANSPORT_BLE,
            TRANSPORT_WIFI_LOCAL,
        )

    @property
    def wifi_local_state(self) -> str:
        """The wifi-local link state: down / passthrough / takeover / reconnecting."""
        proxy = self._wifi_proxy
        if proxy is None:
            return WIFI_LOCAL_DOWN
        mode = proxy.session_mode(self.lock_id)
        if mode is not None:
            # MODE_PASSTHROUGH or MODE_TAKEOVER - a live session.
            return mode
        if proxy.is_reconnecting_client(self.lock_id):
            return WIFI_LOCAL_RECONNECTING
        return WIFI_LOCAL_DOWN

    @property
    def wifi_local_last_seen(self) -> datetime | None:
        """When the lock last had traffic on its wifi-local proxy connection."""
        proxy = self._wifi_proxy
        return proxy.last_seen(self.lock_id) if proxy is not None else None

    @property
    def cloud_online(self) -> bool:
        """Whether the cloud transport can currently reach this lock.

        Cloud must be enabled and the lock cloud-reachable; for a gatewayed
        lock that further means at least one linked gateway is online, where a
        WiFi-capable lock is reachable by the cloud by design.
        """
        if not self.cloud_enabled or not self.cloud_connectable:
            return False
        if self.has_gateway:
            return self.gateway_online
        return True

    @callback
    def async_wifi_local_changed(self) -> None:
        """Let entities re-read wifi-local state when a proxy session changes.

        Called by wifi_proxy when a session is accepted, becomes takeover-ready
        or ends - without it the connection diagnostics could sit a whole poll
        interval stale. Guarded so a listener error never breaks a proxy session.
        """
        try:
            self.async_update_listeners()
            if (self.update_interval is None) == self.connectable:
                self._async_reconcile_polling()
            elif self.wifi_local_drivable and (
                self._state_stale
                or (self._settings_unknown and self._local_settings_due(dt_util.now()))
            ):
                self.hass.async_create_task(self.async_request_refresh())
        except Exception:
            _LOGGER.debug(
                "wifi-local change notification failed for lock %s",
                self.lock_id,
                exc_info=True,
            )

    @callback
    def async_wifi_local_event(self) -> None:
        """The lock reported an event on its session, so its bolt may have moved.

        Requests a (debounced) refresh to read it, and a debounced log read:
        while a log read runs, one more after it, so the event is not missed.
        """
        self.hass.async_create_task(self.async_request_refresh())
        if not self._log_readable_in_background:
            return
        if self._log_reads_running:
            self._log_read_again = True
        else:
            self._log_event_read.async_schedule_call()

    @property
    def _state_stale(self) -> bool:
        """Whether the bolt is unread, or last read longer than a poll ago."""
        return (
            self._verified_at is None
            or dt_util.utcnow() - self._verified_at > self._poll_interval
        )

    @property
    def wifi_local_drivable(self) -> bool:
        """Whether the WiFi-local transport can read or drive the lock for us.

        True for a ready takeover session, and for a recent takeover client whose
        session is momentarily down in a reconnect blip - the proxy then waits
        briefly for it to return rather than skipping straight to the next
        transport. A pass-through session relays to the cloud, so it gives no
        local path and does not count.
        """
        proxy = self._wifi_proxy
        return proxy is not None and (
            proxy.is_takeover_ready(self.lock_id)
            or proxy.is_reconnecting_client(self.lock_id)
        )

    async def async_read_state_wifi_local(
        self,
    ) -> tuple[bool | None, int | None] | None:
        """Read (locked, battery) over the WiFi-local takeover session.

        None when WiFi-local can't serve us or the read didn't land, so the
        caller falls back exactly as a missed Bluetooth read does. Never raises:
        wifi_proxy.async_status swallows a stalled or declined read and keeps
        the session. A reconnecting client is read too, so a poll can wait up
        to wifi_proxy.RECONNECT_WAIT for its session to return.
        """
        proxy = self._wifi_proxy
        if proxy is None or not self.wifi_local_drivable:
            return None
        return await proxy.async_status(self.lock_id)

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

    async def _fetch_credential_kind(
        self, lister: Callable[[int], Any]
    ) -> dict[int, CredentialInfo] | None:
        """List one credential kind as id -> CredentialInfo, or None on failure.

        Best-effort and READ-ONLY: a failure (no such hardware, rate limit, an
        unreachable cloud) returns None so the caller keeps the
        previous mapping for that kind rather than blanking it. Only
        name/type/validity are copied - the secret number is never read or kept.
        """
        try:
            items = await lister(self.lock_id)
        except Exception:
            _LOGGER.debug(
                "Credential list unavailable for lock %s", self.lock_id, exc_info=True
            )
            return None
        result: dict[int, CredentialInfo] = {}
        for item in items:
            if item.id is None:
                continue
            result[item.id] = CredentialInfo(
                name=item.name,
                type=item.type.name if item.type is not None else None,
                valid_from=_validity_or_none(item.start_date),
                valid_until=_validity_or_none(item.end_date),
            )
        return result

    async def _maybe_refresh_credentials(self, now: datetime) -> None:
        """Refresh the id -> name/type/validity maps if the slow tier is due.

        Only with the cloud on, for an admin key (the list endpoints 20002 for
        MEMBER), and only when something uses the names - a local transport
        that manages credentials, or the Credentials sensor - since it costs
        three cloud calls per lock. Best-effort, and stamped on attempt so a
        failing lock doesn't re-hit the endpoints every poll.
        """
        await self.async_restore_credential_names()
        if not (self.cloud_enabled and self.access_level.can_admin):
            return
        if not (
            self.ble_control_enabled
            or self._wifi_local_option
            or self._credentials_entity_enabled
        ):
            return
        if not self._slow_tier_due(self._credentials_last_fetched, now):
            return
        self._credentials_last_fetched = now
        current = self.credential_names
        passcodes = await self._fetch_credential_kind(self.api.list_passcodes)
        cards = await self._fetch_credential_kind(self.api.list_cards)
        fingerprints = await self._fetch_credential_kind(self.api.list_fingerprints)
        updated = CredentialNames(
            passcodes=current.passcodes if passcodes is None else passcodes,
            cards=current.cards if cards is None else cards,
            fingerprints=current.fingerprints if fingerprints is None else fingerprints,
        )
        if updated == current:
            return
        self.credential_names = updated
        await self._persist_credential_names()

    @property
    def _credentials_entity_enabled(self) -> bool:
        """Whether this lock's Credentials sensor (off by default) is enabled."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.unique_id}-lockcredentials"
        )
        entry = registry.async_get(entity_id) if entity_id else None
        return entry is not None and not entry.disabled

    @staticmethod
    def _serialize_credential_kind(
        kind: dict[int, CredentialInfo],
    ) -> dict[str, dict[str, Any]]:
        """JSON-friendly form of one credential map (ISO strings, no secrets)."""
        return {
            str(cid): {
                "name": info.name,
                "type": info.type,
                "valid_from": info.valid_from.isoformat() if info.valid_from else None,
                "valid_until": (
                    info.valid_until.isoformat() if info.valid_until else None
                ),
            }
            for cid, info in kind.items()
        }

    @staticmethod
    def _deserialize_credential_kind(
        raw: dict[str, Any] | None,
    ) -> dict[int, CredentialInfo]:
        """Rebuild one credential map from its persisted form."""
        result: dict[int, CredentialInfo] = {}
        for cid, info in (raw or {}).items():
            with suppress(ValueError, TypeError, AttributeError):
                result[int(cid)] = CredentialInfo(
                    name=info.get("name"),
                    type=info.get("type"),
                    valid_from=dt_util.parse_datetime(info.get("valid_from") or ""),
                    valid_until=dt_util.parse_datetime(info.get("valid_until") or ""),
                )
        return result

    async def _persist_credential_names(self) -> None:
        """Persist the name maps so they survive a restart (names only, no secrets)."""
        await self._store.async_update(
            self.lock_id,
            credential_names={
                "passcodes": self._serialize_credential_kind(
                    self.credential_names.passcodes
                ),
                "cards": self._serialize_credential_kind(self.credential_names.cards),
                "fingerprints": self._serialize_credential_kind(
                    self.credential_names.fingerprints
                ),
            },
        )

    async def async_restore_credential_names(self) -> None:
        """Load persisted credential names once, if the in-memory map is empty.

        Lets management show names straight after a restart, and with the cloud
        off. No-op after the first attempt or once names are already in memory.
        """
        if self._credentials_restored:
            return
        self._credentials_restored = True
        if any(self.credential_names.counts.values()):
            return
        stored = (await self._store.async_get(self.lock_id)).get("credential_names")
        if not stored:
            return
        self.credential_names = CredentialNames(
            passcodes=self._deserialize_credential_kind(stored.get("passcodes")),
            cards=self._deserialize_credential_kind(stored.get("cards")),
            fingerprints=self._deserialize_credential_kind(stored.get("fingerprints")),
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
        if self.cloud_enabled:
            await self._async_refresh_slow_tier(new_data, now)
        await self._async_refresh_settings_locally(new_data, now)
        if Features.door_sensor in new_data.features:
            # a placeholder for the door state if the lock supports one
            if new_data.sensor is None:
                new_data.sensor = SensorData()
            if self.cloud_enabled:
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
            status = await self.async_read_lock_status(cloud=self.cloud_enabled)
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
        """Read the bolt over BLE and WiFi-local in `transport_order`, then the cloud.

        The cloud is asked last, and only if `cloud`. None when no local
        transport answered and the cloud wasn't asked. A cloud read that fails
        raises; the cloud's "could not read" (`2`) is a read whose bolt is None
        - a bolt we could not read is not an unlocked one.
        """
        for transport in self._transport_order:
            if transport == TRANSPORT_BLE:
                status = await self.async_read_state_ble()
                if status is not None and status.locked is not None:
                    return status
            elif transport == TRANSPORT_WIFI_LOCAL:
                wifi = await self.async_read_state_wifi_local()
                if wifi is not None and wifi[0] is not None:
                    return LockStatus(battery=wifi[1], locked=wifi[0], door_open=None)
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
            self._cloud_detail_ok = False
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
                self._cloud_detail_ok = True

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

        await self._maybe_refresh_credentials(now)

    @property
    def _settings_unknown(self) -> bool:
        """Whether any saved setting an entity shows is still unknown."""
        data = self.data
        return (
            None in (data.auto_lock_seconds, data.lock_sound)
            or (Features.tamper_alert in data.features and data.tamper_alert is None)
            or (
                Features.passage_mode in data.features
                and data.passage_auto_unlock is None
            )
        )

    @property
    def _local_admin_route(self) -> bool:
        """Whether a local transport could carry an admin op right now."""
        ble = (
            self.ble_control_enabled
            and self.ble_reachable
            and self._ble_control_credentials() is not None
            and self.admin_ps is not None
        )
        return ble or (self.wifi_local_drivable and self.wifi_credentials() is not None)

    def _local_settings_due(self, now: datetime) -> bool:
        """Whether to read the saved settings from the lock now.

        Only when the cloud has not supplied them (off, or its last lock/detail
        fetch failed), the key can administer the lock, a local route is up,
        and no read was made this slow interval.
        """
        return (
            not (self.cloud_enabled and self._cloud_detail_ok)
            and self.access_level.can_admin
            and self._slow_tier_due(self._local_settings_last_read, now)
            and self._local_admin_route
        )

    async def _async_refresh_settings_locally(
        self, new_data: LockState, now: datetime
    ) -> None:
        """Read the saved settings from the lock when the cloud can't supply them.

        Stamped only when a value is read, so a lock that does not answer is
        asked again on the next poll. A failure logs and keeps the values
        already read; it never fails the poll.
        """
        if not self._local_settings_due(now):
            return
        read: dict[str, Any] = {}

        async def op(session: lock_commands.AdminSession) -> None:
            await _read_settings(session, read, new_data.features)

        try:
            await self.async_run_local_admin_op(
                "read settings",
                op,
                idempotent=True,
                read_only=True,
                log_level=logging.DEBUG,
            )
        except Exception as err:  # noqa: BLE001 - never fail the poll over settings
            _LOGGER.debug(
                "Could not read the settings of lock %s (%s); keeping the values read",
                self.lock_id,
                type(err).__name__,
            )
        if read:
            self._local_settings_last_read = now
            self._apply_detail(new_data, LockDetail.model_validate(read))
            await self._persist_detail(**read)

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
        """Cancel any pending auto-lock timer and log read with scheduled refreshes."""
        self._cancel_auto_lock()
        self._log_event_read.async_shutdown()
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
            status = await self.async_read_lock_status(cloud=self.cloud_enabled)
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
        # The lock carries both its BLE MAC (the cloud lockMac) and, for a WiFi
        # lock, its derived WiFi client MAC. A shared connection never merges
        # devices across config entries; the link is via_device, below.
        connections: set[tuple[str, str]] = set()
        wifi_mac: str | None = None
        if self.data.mac:
            connections.add((dr.CONNECTION_BLUETOOTH, dr.format_mac(self.data.mac)))
            if Features.wifi in self.data.features and (
                wifi_mac := _wifi_mac_from_ble(self.data.mac)
            ):
                connections.add((dr.CONNECTION_NETWORK_MAC, dr.format_mac(wifi_mac)))
        info = DeviceInfo(
            identifiers={(DOMAIN, self.data.mac)},
            connections=connections,
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
        # The gateway link takes precedence (it is the TTLock topology). A lock
        # without one is linked under whichever device another integration (a
        # router or access point, say) registered for its WiFi MAC, if any.
        if "via_device_id" not in info and wifi_mac is not None:
            network_id = self._wifi_via_device_id(wifi_mac)
            if network_id is not None:
                info["via_device_id"] = network_id
        return info

    def _wifi_via_device_id(self, wifi_mac: str) -> str | None:
        """The id of another integration's device holding this lock's WiFi MAC.

        The ttlock device declares that same connection, so a single-match
        lookup can return our own device; all devices are scanned instead and
        our own (matched by identifier) is skipped. Returns None (link omitted)
        when no other device holds the MAC yet.
        """
        connection = (dr.CONNECTION_NETWORK_MAC, dr.format_mac(wifi_mac))
        with suppress(Exception):
            registry = dr.async_get(self.hass)
            # DeviceRegistryItems is a UserDict; .values() yields DeviceEntry,
            # which the stub's Collection type hides - hence the narrow ignore.
            for device in registry.devices.values():  # ty: ignore[unresolved-attribute]
                if (
                    connection in device.connections
                    and (DOMAIN, self.data.mac) not in device.identifiers
                ):
                    return device.id
        return None

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
            "cloud_enabled": self.cloud_enabled,
            "transport_order": self._transport_order,
            "wifi_local_drivable": self.wifi_local_drivable,
            "wifi_local_state": self.wifi_local_state,
            "active_transport": self.active_transport,
            "last_transport": self.last_transport,
            "control_cloud_free": self.control_cloud_free,
            "cloud_online": self.cloud_online,
            # Counts only - names are deliberately left out of the shareable
            # diagnostics export (and secret digits are never stored at all).
            "credential_counts": self.credential_names.counts,
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
        and reachable), wifi_local (a live proxy session, or a recent
        reconnecting client whose session the control path then waits briefly
        to return) and cloud (cloud_enabled). A transport that fails, by
        declining or by raising, is logged and the next is tried. If nothing is
        available, or everything tried failed, it raises so an automation never
        takes a failed lock or unlock for a done one.
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
            if not tried:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=f"no_transport_to_{action}",
                    translation_placeholders={"name": self.data.name},
                )
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
        if transport == TRANSPORT_WIFI_LOCAL:
            if not self.wifi_local_drivable:
                return None
            return lambda: self._async_control_wifi_local(unlock=unlock)
        # The cloud: `transport_order` admits no other name.
        if not self.cloud_enabled:
            return None
        return lambda: self._async_control_cloud(unlock=unlock)

    async def _async_control_wifi_local(self, *, unlock: bool) -> bool:
        """Drive the bolt over the lock's live proxy session; False on failure."""
        proxy = self._wifi_proxy
        if proxy is None:
            return False
        if unlock:
            return await proxy.async_unlock(self.lock_id)
        return await proxy.async_lock(self.lock_id)

    async def _async_control_cloud(self, *, unlock: bool) -> bool:
        """Drive the bolt over the TTLock cloud; False if the cloud declined."""
        if unlock:
            return bool(await self.api.unlock(self.lock_id))
        return bool(await self.api.lock(self.lock_id))

    async def set_auto_lock(self, on: bool) -> None:
        """Turn Autolock on (10s) or off, over the best available transport."""
        await self.set_auto_lock_seconds(10 if on else 0)

    async def set_auto_lock_seconds(self, seconds: int) -> None:
        """Set the auto-lock delay in seconds (0 disables), local-first.

        Tries BLE / WiFi-local via the admin handshake, then the cloud. On
        success the cached delay is updated; disabling cancels any pending
        assume-auto-locked countdown.
        """

        async def op(session: lock_commands.AdminSession) -> AutoLockConfig:
            return await lock_commands.set_auto_lock(session, seconds)

        applied = seconds
        try:
            config = await self.async_run_local_admin_op(
                "set auto-lock", op, idempotent=True
            )
        except NoLocalAdminTransport:
            if not self.cloud_enabled:
                raise
            if not await self.api.set_auto_lock(self.lock_id, seconds):
                return
        else:
            # The local set re-reads: trust the lock's own value.
            if config.seconds is not None:
                applied = config.seconds
        self.data.auto_lock_seconds = applied
        await self._persist_detail(autoLockTime=applied)
        if applied == 0:
            # a countdown started under the old setting no longer applies
            self._cancel_auto_lock()
        self.async_update_listeners()

    @property
    def _lock_tz(self) -> tzinfo | None:
        """The lock's own timezone, which every date written to it is rendered in.

        An aware datetime is converted to it; a naive one is sent as given.
        """
        return lock_timezone(self._timezone_raw_offset)

    async def set_lock_sound(self, on: bool) -> None:
        """Turn the lock's prompt sound on/off, local-first then cloud."""

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.set_lock_sound(session, on=on)

        try:
            await self.async_run_local_admin_op("set lock-sound", op, idempotent=True)
        except NoLocalAdminTransport:
            if not self.cloud_enabled:
                raise
            # Cloud uses 1=on / 2=off, not the wire's 1/0.
            if not await self.api.set_lock_sound(self.lock_id, 1 if on else 2):
                return
        self.data.lock_sound = on
        await self._persist_detail(lockSound=_on_off(on))
        self.async_update_listeners()

    async def sync_clock(self, moment: datetime) -> None:
        """Write `moment` to the lock's clock, local-only.

        There is no cloud calibrate endpoint, so this needs BLE or WiFi-local;
        `NoLocalAdminTransport` propagates when neither is available.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.sync_clock(session, moment, tz=self._lock_tz)

        await self.async_run_local_admin_op("sync clock", op, idempotent=True)

    async def add_passcode_local(
        self,
        code: str,
        *,
        name: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        """Add a passcode over a local transport (PERMANENT, or timed if windowed).

        On success the user-given `name` is recorded in the credential-name cache
        so the lock's management surface shows it, since the 0x03 wire add carries
        no name (names are HA/cloud metadata). The secret digits are never stored.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.add_passcode(
                session, code, start=start, end=end, tz=self._lock_tz
            )

        await self.async_run_local_admin_op("add passcode", op, idempotent=False)
        if name is not None:
            await self._record_local_name("passcodes", name, start=start, end=end)

    async def _record_local_name(
        self,
        kind: str,
        name: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        cred_type: str | None = None,
    ) -> None:
        """Keep a locally-added credential's name under a synthetic id.

        A BLE/WiFi add returns no id and carries no name, so the name is kept
        display-only under a negative id that cannot collide with the cloud's
        positive ids - never the secret code or number. With the cloud on, the
        next credential refresh replaces the map with the cloud's own list.
        `kind` is a CredentialNames field; `cred_type` overrides the display
        type, else PERIOD (windowed) or PERMANENT.
        """
        names = dict(getattr(self.credential_names, kind))
        names[min([0, *names]) - 1] = CredentialInfo(
            name=name,
            type=cred_type or ("PERIOD" if (start and end) else "PERMANENT"),
            valid_from=start,
            valid_until=end,
        )
        await self._set_credential_names(kind, names)

    async def _drop_local_names(self, kind: str) -> None:
        """Forget the locally-recorded names of one kind once the lock cleared it.

        Cloud-sourced names are left for the next refresh.
        """
        names = getattr(self.credential_names, kind)
        kept = {cid: info for cid, info in names.items() if cid >= 0}
        if kept != names:
            await self._set_credential_names(kind, kept)

    async def _set_credential_names(
        self, kind: str, names: dict[int, CredentialInfo]
    ) -> None:
        self.credential_names = replace(self.credential_names, **{kind: names})
        await self._persist_credential_names()
        self.async_update_listeners()

    async def delete_passcode_local(self, code: str, *, pwd_type: int) -> None:
        """Delete one passcode by value over a local transport."""

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.delete_passcode(session, code, pwd_type=pwd_type)

        await self.async_run_local_admin_op("delete passcode", op, idempotent=False)

    async def add_onetime_passcode_local(
        self,
        code: str,
        *,
        name: str | None = None,
        start: datetime,
        end: datetime,
    ) -> None:
        """Add a one-time / use-count (COUNT) passcode over a local transport.

        Local-only (the COUNT type is not wired to the cloud fallback). Both bounds
        are required. On success the name is recorded for display; the digits are
        never stored.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.add_onetime_passcode(
                session, code, start, end, tz=self._lock_tz
            )

        await self.async_run_local_admin_op(
            "add one-time passcode", op, idempotent=False
        )
        if name is not None:
            await self._record_local_name(
                "passcodes", name, start=start, end=end, cred_type="COUNT"
            )

    async def add_cyclic_passcode_local(
        self,
        code: str,
        *,
        name: str | None = None,
        start: datetime,
        cycle_type: int,
    ) -> None:
        """Add a CIRCLE (cyclic) passcode over a local transport (op=6 RECOVERY path).

        Local-only. `cycle_type` is the raw 2-byte selector; only the HH:MM of
        `start` is the daily window-open time. On success the name is recorded for
        display; the digits are never stored.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.add_cyclic_passcode(
                session, code, start=start, cycle_type=cycle_type, tz=self._lock_tz
            )

        await self.async_run_local_admin_op("add cyclic passcode", op, idempotent=False)
        if name is not None:
            await self._record_local_name(
                "passcodes", name, start=start, cred_type="CYCLIC"
            )

    async def modify_passcode_local(
        self,
        old_code: str,
        new_code: str = "",
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        pwd_type: int,
    ) -> None:
        """Change a passcode's value and/or validity over a local transport (op 5).

        Local-only. No credential-name cache update: a local add stores no code, so
        the entry for `old_code` cannot be located. Some firmware ignores MODIFY
        (prefer delete + re-add).
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.modify_passcode(
                session,
                old_code,
                new_code,
                start=start,
                end=end,
                pwd_type=pwd_type,
                tz=self._lock_tz,
            )

        await self.async_run_local_admin_op("modify passcode", op, idempotent=False)

    async def clear_passcodes_local(self) -> None:
        """Clear every passcode over a local transport.

        On success the locally-recorded passcode names (synthetic negative ids)
        are dropped too, so management does not keep showing names for codes the
        lock no longer holds. Cloud-sourced names are left for the next refresh.
        """
        await self.async_run_local_admin_op(
            "clear passcodes", lock_commands.clear_passcodes, idempotent=True
        )
        await self._drop_local_names("passcodes")

    # --- IC / RFID cards (local transport) ---------------------------------

    async def add_card_local(
        self,
        card_number: str,
        *,
        name: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        """Add an IC card by number over a local transport (permanent or timed).

        On success the user-given `name` is recorded in the credential-name cache
        (the 0x05 wire add carries no name). The raw card number is NEVER stored in
        state or logs - only the name and validity window are kept.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.add_card_by_number(
                session, card_number, start=start, end=end, tz=self._lock_tz
            )

        await self.async_run_local_admin_op("add card", op, idempotent=False)
        if name is not None:
            await self._record_local_name("cards", name, start=start, end=end)

    async def delete_card_local(self, card_number: str) -> None:
        """Delete one IC card by its number over a local transport."""

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.delete_card(session, card_number)

        await self.async_run_local_admin_op("delete card", op, idempotent=False)

    async def clear_cards_local(self) -> None:
        """Clear every IC card over a local transport (no cloud/gateway equivalent).

        On success the locally-recorded card names (synthetic negative ids) are
        dropped too, so management does not keep showing names for cards the lock
        no longer holds. Cloud-sourced names are left for the next refresh.
        """
        await self.async_run_local_admin_op(
            "clear cards", lock_commands.clear_cards, idempotent=True
        )
        await self._drop_local_names("cards")

    async def modify_card_validity_local(
        self, card_number: str, start: datetime, end: datetime
    ) -> None:
        """Change an IC card's validity window over a local transport.

        Some firmware ignores MODIFY (prefer delete + re-add).
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.modify_card_validity(
                session, card_number, start, end, tz=self._lock_tz
            )

        await self.async_run_local_admin_op("modify card", op, idempotent=False)

    async def set_card_schedule_local(
        self,
        card_number: str,
        *,
        week_days: list[int],
        start_minute: int,
        end_minute: int,
    ) -> None:
        """Set an IC card's weekly schedule (0x70) over a local transport.

        The card must already exist. Clears the card's existing schedule first,
        then adds one entry per weekday - all in a single admin session so the
        lock sees one coherent rewrite.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.clear_card_cyclic(session, card_number)
            for week_day in week_days:
                await lock_commands.add_card_cyclic(
                    session,
                    card_number,
                    week_day=week_day,
                    start_minute=start_minute,
                    end_minute=end_minute,
                )

        await self.async_run_local_admin_op("set card schedule", op, idempotent=False)

    async def clear_card_cyclic_local(self, card_number: str) -> None:
        """Clear an IC card's weekly schedule (0x70) over a local transport."""

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.clear_card_cyclic(session, card_number)

        await self.async_run_local_admin_op("clear card schedule", op, idempotent=True)

    # --- fingerprints (local transport) ------------------------------------

    async def enroll_fingerprint_local(self, *, name: str | None = None) -> int:
        """Enrol a fingerprint by scanning, over the WiFi-local takeover session.

        Enrolment is multi-frame (the lock streams scan progress then the number),
        so it is WiFi-local ONLY: BLE's session is one round-trip per command and
        the cloud has no equivalent. Raises `NoLocalAdminTransport` if there is no
        drivable WiFi-local session - before anything is sent - so the lock is never
        left half in add mode. On success the given `name` is recorded under a
        synthetic negative id (as cards/passcodes are); the returned number is the
        only place the raw number is surfaced, and it is never logged or stored.
        """
        proxy = self._wifi_proxy
        if (
            proxy is None
            or not self.wifi_local_drivable
            or self.wifi_credentials() is None
        ):
            raise NoLocalAdminTransport(self.data.name)
        try:
            async with self._admin_turn(read_only=False), self._local_lock:
                attempted, number = await proxy.async_enroll_fingerprint(self.lock_id)
        except CommandRejected as err:
            raise self._rejected(err) from err
        except Exception as err:
            # possibly mid-enrolment: never retried elsewhere
            raise self._outcome_unknown() from err
        if not attempted or number is None:
            raise NoLocalAdminTransport(self.data.name)
        if name is not None:
            await self._record_local_name("fingerprints", name)
        return number

    async def delete_fingerprint_local(self, fingerprint_number: str) -> None:
        """Delete one fingerprint by its number over a local transport."""

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.delete_fingerprint(session, fingerprint_number)

        await self.async_run_local_admin_op("delete fingerprint", op, idempotent=False)

    async def clear_fingerprints_local(self) -> None:
        """Clear every fingerprint over a local transport (no cloud/gateway path).

        On success the locally-recorded fingerprint names (synthetic negative ids)
        are dropped too; cloud-sourced names are left for the next refresh.
        """
        await self.async_run_local_admin_op(
            "clear fingerprints", lock_commands.clear_fingerprints, idempotent=True
        )
        await self._drop_local_names("fingerprints")

    async def modify_fingerprint_validity_local(
        self, fingerprint_number: str, start: datetime, end: datetime
    ) -> None:
        """Change a fingerprint's validity window over a local transport.

        Some firmware ignores MODIFY (prefer delete + re-enrol).
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.modify_fingerprint_validity(
                session, fingerprint_number, start, end, tz=self._lock_tz
            )

        await self.async_run_local_admin_op("modify fingerprint", op, idempotent=False)

    async def set_fingerprint_schedule_local(
        self,
        fingerprint_number: str,
        *,
        week_days: list[int],
        start_minute: int,
        end_minute: int,
    ) -> None:
        """Set a fingerprint's weekly schedule (0x70) over a local transport.

        The fingerprint must already exist. Clears its existing schedule first, then
        adds one entry per weekday - all in a single admin session.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.clear_fingerprint_cyclic(session, fingerprint_number)
            for week_day in week_days:
                await lock_commands.add_fingerprint_cyclic(
                    session,
                    fingerprint_number,
                    week_day=week_day,
                    start_minute=start_minute,
                    end_minute=end_minute,
                )

        await self.async_run_local_admin_op(
            "set fingerprint schedule", op, idempotent=False
        )

    async def clear_fingerprint_cyclic_local(self, fingerprint_number: str) -> None:
        """Clear a fingerprint's weekly schedule (0x70) over a local transport."""

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.clear_fingerprint_cyclic(session, fingerprint_number)

        await self.async_run_local_admin_op(
            "clear fingerprint schedule", op, idempotent=True
        )

    # --- switches / config bitmask (0x68, local transport) ------------------

    async def set_tamper_alert(self, on: bool) -> None:
        """Turn the tamper alarm on/off over a local transport (0x68, local-only).

        There is no cloud setter for the switch bitmask, so this needs BLE or
        WiFi-local; `NoLocalAdminTransport` propagates when neither is available.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.set_tamper_alert(session, on=on)

        await self.async_run_local_admin_op("set tamper alert", op, idempotent=False)
        self.data.tamper_alert = on
        await self._persist_detail(tamperAlert=_on_off(on))
        self.async_update_listeners()

    async def set_passage_auto_unlock(self, on: bool) -> None:
        """Turn passage-mode auto-unlock on/off over a local transport (0x68).

        Local-only (no cloud switch setter); `NoLocalAdminTransport` propagates
        when no local transport is available.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.set_passage_auto_unlock(session, on=on)

        await self.async_run_local_admin_op(
            "set passage auto-unlock", op, idempotent=False
        )
        self.data.passage_auto_unlock = on
        await self._persist_detail(passageModeAutoUnlock=_on_off(on))
        self.async_update_listeners()

    # --- passage mode (0x66, local transport) -------------------------------

    async def set_passage_mode_local(
        self,
        *,
        week_days: list[int],
        start_minute: int,
        end_minute: int,
        all_day: bool,
        auto_unlock: bool | None = None,
    ) -> None:
        """Enable passage mode over a local transport (0x66, local variant).

        Distinct from the cloud `configure_passage_mode` service: this drives the
        lock directly (CLEAR then one ADD per weekday, then the optional 0x68
        auto-unlock), all in one admin session.
        """

        async def op(session: lock_commands.AdminSession) -> None:
            await lock_commands.set_passage_mode(
                session,
                week_days=week_days,
                start_minute=start_minute,
                end_minute=end_minute,
                all_day=all_day,
                auto_unlock=auto_unlock,
            )

        await self.async_run_local_admin_op("set passage mode", op, idempotent=False)
        # Keep the cached config in step so the passage-mode binary sensor reflects
        # the local change at once (as the cloud service does), not only after the
        # next cloud poll.
        self.data.passage_mode_config = PassageModeConfig(
            passageMode=OnOff.on,
            isAllDay=OnOff.on if all_day else OnOff.off,
            startDate=start_minute,
            endDate=end_minute,
            weekDays=list(week_days),
            autoUnlock=(
                OnOff.unknown
                if auto_unlock is None
                else (OnOff.on if auto_unlock else OnOff.off)
            ),
        )
        await self._persist_passage_mode_config()
        if auto_unlock is not None:
            self.data.passage_auto_unlock = auto_unlock
            await self._persist_detail(passageModeAutoUnlock=_on_off(auto_unlock))
        self.async_update_listeners()

    async def clear_passage_mode_local(self) -> None:
        """Disable passage mode over a local transport (0x66 CLEAR, local-only)."""
        await self.async_run_local_admin_op(
            "clear passage mode", lock_commands.clear_passage_mode, idempotent=True
        )
        if self.data.passage_mode_config is not None:
            self.data.passage_mode_config = PassageModeConfig(passageMode=OnOff.off)
            await self._persist_passage_mode_config()
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
