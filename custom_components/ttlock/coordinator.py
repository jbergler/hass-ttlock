"""Provides the TTLock LockUpdateCoordinator.

State reaches HA two ways: polling and webhook push, both merging into
LockUpdateCoordinator. This module owns polling — LockUpdateCoordinator (per
lock) and GatewaysUpdateCoordinator (gateway online/offline), each on a
15-minute interval. LockUpdateCoordinator also listens for SIGNAL_NEW_DATA
(dispatched from webhook.py) via _process_webhook_data, which is why lock
state updates are close to real-time rather than poll-only — don't remove
the webhook path in favor of "just poll faster."
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TypeGuard

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TTLockApi
from .ble import (
    BleData,
    BleError,
    async_bluetooth_available,
    async_read_status,
    async_track_address,
)
from .ble_protocol import LockStatus, LockVersion, ProtocolError, parse_aes_key
from .capture import LockTrafficCapture
from .const import (
    CONF_POLL_INTERVAL,
    CONF_SLOW_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_SLOW_POLL_INTERVAL_HOURS,
    DOMAIN,
    SIGNAL_NEW_DATA,
    TT_GATEWAYS,
    TT_LOCKS,
)
from .models import (
    Features,
    Gateway,
    GatewayLink,
    LockSummary,
    PassageModeConfig,
    Sensor,
    SensorState,
    State,
    WebhookEvent,
)
from .store import LockStateStore

_LOGGER = logging.getLogger(__name__)

NOT_CONNECTABLE_REASON = (
    "No gateway paired and no WiFi — TTLock's cloud can't reach this lock. "
    "Live data isn't available until connectivity is restored."
)

# TTLock's doorSensor/query can't distinguish "no sensor paired" from a
# transient failure (see api.get_sensor), so a lock only counts as confirmed
# absent after this many consecutive failed checks - see
# LockUpdateCoordinator._check_for_sensor.
SENSOR_ABSENT_AFTER_FAILURES = 3


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
    """Check if a sensor is present."""
    return instance is not None and instance.present


def async_add_when_sensor_present(
    coordinator: LockUpdateCoordinator, add_entities: Callable[[], None]
) -> None:
    """Call `add_entities` once presence is confirmed - now, or later.

    Door-sensor presence isn't known at the cheap lock/list stage entities
    are created from at platform setup - it's only filled in once the
    coordinator's first refresh completes, which __init__.py runs in the
    background *after* platforms are set up (see _fill_lock_details), so
    sensor-backed entities can't just check coordinator.data.sensor at setup
    time. Instead: add immediately if presence is already known, otherwise
    watch for the update that confirms it and add then.
    """
    if sensor_present(coordinator.data.sensor):
        add_entities()
        return

    remove: Callable[[], None] | None = None

    @callback
    def _check_and_add() -> None:
        if sensor_present(coordinator.data.sensor):
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
        self.connectable = summary.connectable
        self.has_gateway = summary.hasGateway
        self.feature_value = summary.featureValue
        self._capture = capture
        self._store = store

        # Latest passive Bluetooth presence for this lock.
        # Empty until async_start_ble_tracking hears the first
        # advertisement (or immediately, if the stack already has one)
        self.ble: BleData = BleData()

        # What lock/detail told us about talking to the lock directly. Kept
        # as received; parsed on use so an odd value costs a local read, not
        # the poll.
        self._ble_version: dict[str, int] | None = None
        self._ble_key: str | None = None

        # Whether we've already made this restart's one-shot door-sensor
        # recheck (see _check_for_sensor) - deliberately in-memory, not
        # persisted, so it naturally resets to False on every restart.
        self._sensor_recheck_done = False

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
        self._slow_interval = timedelta(hours=slow_hours)
        self._details_last_fetched: datetime | None = None
        self._passage_last_fetched: datetime | None = None
        self._gateways_last_fetched: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=(
                timedelta(minutes=poll_minutes) if self.connectable else None
            ),
        )

        self.data = LockState(
            name=summary.name, mac=summary.mac, features=summary.features
        )

        async_dispatcher_connect(self.hass, SIGNAL_NEW_DATA, self._process_webhook_data)

    @callback
    def async_start_ble_tracking(self) -> CALLBACK_TYPE:
        """Begin watching this lock's Bluetooth presence.

        Returns an unsubscribe callable for the caller to register with
        `entry.async_on_unload`. When the bluetooth component isn't set up
        there is nothing to watch, so this is a no-op that returns a no-op -
        the lock keeps working over the cloud exactly as before.
        """
        if not async_bluetooth_available(self.hass):
            return lambda: None
        return async_track_address(self.hass, self.data.mac, self._async_ble_updated)

    @callback
    def _async_ble_updated(self, ble: BleData) -> None:
        """Record new presence and let entities re-read it.

        This is passive - a presence change never triggers a cloud poll or a
        connection, it only refreshes what the RSSI sensor reports.
        """
        self.ble = ble
        self.async_update_listeners()

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

        Never raises - the caller has the cloud to fall back on, and a lock that
        is out of range or not answering is expected, not exceptional.
        """
        if not async_bluetooth_available(self.hass) or not self.ble.in_range:
            return None
        credentials = self._ble_credentials()
        if credentials is None:
            return None
        version, aes_key = credentials
        try:
            return await async_read_status(
                self.hass, self.data.mac, self.data.name, version, aes_key
            )
        except (BleError, ProtocolError) as err:
            _LOGGER.debug("Local state read for lock %s failed: %s", self.lock_id, err)
            return None

    async def _async_update_data(self) -> LockState:
        if not self.connectable:
            raise UpdateFailed(
                f"Lock {self.lock_id} has no gateway or WiFi connectivity"
            )

        try:
            now = dt_util.now()
            new_data = deepcopy(self.data)

            # Slow tier: full lock detail (battery, name, model, features,
            # autolock/sound config). Slow-changing and, for lock/unlock,
            # already delivered in real time by webhooks - so fetch it at most
            # once per slow interval and otherwise carry the previous values
            # forward via the deepcopy above.
            if self._slow_tier_due(self._details_last_fetched, now):
                details = await self.api.get_lock(self.lock_id)
                new_data.name = details.name
                new_data.mac = details.mac
                new_data.model = details.model
                new_data.features = Features.from_feature_value(details.featureValue)
                new_data.battery_level = details.battery_level
                new_data.hardware_version = details.hardwareRevision
                new_data.firmware_version = details.firmwareRevision
                new_data.auto_lock_seconds = details.autoLockTime
                new_data.lock_sound = bool(details.lockSound)
                self._ble_version = details.lockVersion
                self._ble_key = details.aesKeyStr
                self._details_last_fetched = now

            if Features.door_sensor in new_data.features:
                # make sure we have a placeholder for sensor state if the lock supports it
                if new_data.sensor is None:
                    new_data.sensor = SensorData()

                if sensor_present(new_data.sensor):
                    await self._update_present_sensor(new_data.sensor)
                else:
                    await self._check_for_sensor(new_data.sensor)
            else:
                new_data.sensor = None

            # Fast tier: re-verify lock (and door) state every poll - this is
            # the real-time-relevant signal and the reason the poll exists.
            # A lock in Bluetooth range answers the same question locally,
            # without spending cloud quota; the cloud remains the fallback.
            status = await self.async_read_state_ble()
            if status is not None and status.locked is not None:
                new_data.locked = status.locked
                if status.battery is not None:
                    new_data.battery_level = status.battery
                if sensor_present(new_data.sensor) and status.door_open is not None:
                    new_data.sensor.opened = status.door_open
            else:
                try:
                    state = await self.api.get_lock_state(self.lock_id)
                    new_data.locked = state.locked == State.locked
                    if sensor_present(new_data.sensor):
                        new_data.sensor.opened = state.opened == SensorState.opened
                except Exception as err:
                    if new_data.locked is None:
                        # first-ever fetch: tolerate failure, entity shows "unknown"
                        _LOGGER.debug(
                            "Failed to fetch initial lock state", exc_info=True
                        )
                    else:
                        # we previously had a verified state - a failure to re-verify now
                        # means we can no longer trust it (e.g. gateway/webhooks silently
                        # died), so surface it via the standard unavailable path
                        raise UpdateFailed(
                            f"Failed to re-verify lock state for {self.lock_id}"
                        ) from err

            # Slow tier: passage-mode schedule config, rarely changes.
            if self._slow_tier_due(self._passage_last_fetched, now):
                new_data.passage_mode_config = (
                    await self.api.get_lock_passage_mode_config(self.lock_id)
                )
                self._passage_last_fetched = now

            # Slow tier: gateway link (RSSI/name/mac for the signal sensor).
            if self.has_gateway:
                if self._slow_tier_due(self._gateways_last_fetched, now):
                    new_data.gateways = await self.api.get_gateways_for_lock(
                        self.lock_id
                    )
                    self._gateways_last_fetched = now
            else:
                new_data.gateways = []
        except Exception as err:
            raise UpdateFailed(err) from err
        else:
            return new_data

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
            info = self.device_info
            # Resolve the gateway link ourselves and set via_device_id rather
            # than passing the identifier tuple as `via_device` to
            # async_get_or_create - that parameter is deprecated and removed in
            # HA 2027.8.0. If the gateway device isn't registered yet, leave the
            # link untouched, mirroring async_get_or_create's own behaviour.
            via_device = info.pop("via_device", None)
            device = registry.async_get_or_create(
                config_entry_id=self.config_entry.entry_id, **info
            )
            if via_device is not None:
                gateway = registry.async_get_device(identifiers={via_device})
                if gateway is not None:
                    registry.async_update_device(device.id, via_device_id=gateway.id)

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
            elif state.locked == State.unlocked:
                new_data.locked = False
                self._handle_auto_lock(event.lock_ts, event.server_ts)

            if state.locked is not None:
                new_data.last_user = event.user
                new_data.last_reason = event.event.description

        if new_data.sensor and new_data.sensor.present and event.sensorState:
            if event.sensorState.opened == SensorState.opened:
                new_data.sensor.opened = True
            if event.sensorState.opened == SensorState.closed:
                new_data.sensor.opened = False
                new_data.locked = True
                new_data.last_reason = "Door Closed"

                _LOGGER.debug("Assuming auto-locked via sensor")
        self.async_set_updated_data(new_data)

    def _handle_auto_lock(self, lock_ts: datetime, server_ts: datetime):
        """Handle auto-locking the lock."""

        auto_lock_delay = self.data.auto_lock_delay(lock_ts)
        computed_msg_delay = max(0, (server_ts - lock_ts).total_seconds())

        if auto_lock_delay is None:
            _LOGGER.debug("Auto-lock is disabled")

            return

        async def _auto_locked(seconds: int, offset: float = 0):
            if seconds > 0 and (seconds - offset) > 0:
                await asyncio.sleep(seconds - offset)

            new_data = deepcopy(self.data)
            new_data.locked = True
            new_data.last_reason = "Auto Lock"

            _LOGGER.debug("Assuming lock auto locked after %s seconds", auto_lock_delay)
            self.async_set_updated_data(new_data)

        self.hass.create_task(_auto_locked(auto_lock_delay, computed_msg_delay))

    @property
    def unique_id(self) -> str:
        """Unique ID prefix for all entities for the lock."""
        return f"{DOMAIN}-{self.lock_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Device info for the lock.

        via_device links to the best-RSSI gateway currently in range (see
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
        if best_gateway := self.data.best_gateway:
            info["via_device"] = (DOMAIN, best_gateway.mac)
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
            "has_gateway": self.has_gateway,
            "feature_value": self.feature_value,
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
        """Try to lock the lock."""
        with lock_action(self):
            res = await self.api.lock(self.lock_id)
            if res:
                self.data.locked = True

    async def unlock(self) -> None:
        """Try to unlock the lock."""
        with lock_action(self):
            res = await self.api.unlock(self.lock_id)
            if res:
                self.data.locked = False

    async def set_auto_lock(self, on: bool) -> None:
        """Turn on/off Autolock."""
        seconds = 10 if on else 0
        res = await self.api.set_auto_lock(self.lock_id, seconds)
        if res:
            self.data.auto_lock_seconds = seconds
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
    ) -> None:
        """Initialize the update co-ordinator for gateways."""
        self.api = api

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-gateways",
            config_entry=config_entry,
            update_interval=timedelta(minutes=15),
        )

    async def _async_update_data(self) -> dict[int, Gateway]:
        try:
            gateways = await self.api.get_gateways()
            return {gateway.id: gateway for gateway in gateways}
        except Exception as err:
            raise UpdateFailed(err) from err

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
