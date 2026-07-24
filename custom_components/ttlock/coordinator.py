"""Provides the TTLock LockUpdateCoordinator."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TypeGuard

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TTLockApi
from .const import DOMAIN, SIGNAL_NEW_DATA, TT_GATEWAYS, TT_LOCKS
from .models import (
    Features,
    Gateway,
    LockSummary,
    PassageModeConfig,
    SensorState,
    State,
    WebhookEvent,
)

_LOGGER = logging.getLogger(__name__)

NOT_CONNECTABLE_REASON = (
    "No gateway paired and no WiFi — TTLock's cloud can't reach this lock. "
    "Live data isn't available until connectivity is restored."
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
    ) -> None:
        """Initialize the update co-ordinator for a single lock."""
        self.api = api
        self.lock_id = summary.id
        self.connectable = summary.connectable
        self.has_gateway = summary.hasGateway
        self.feature_value = summary.featureValue

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(minutes=15) if self.connectable else None,
        )

        self.data = LockState(
            name=summary.name, mac=summary.mac, features=summary.features
        )

        async_dispatcher_connect(self.hass, SIGNAL_NEW_DATA, self._process_webhook_data)

    async def _async_update_data(self) -> LockState:
        if not self.connectable:
            raise UpdateFailed(
                f"Lock {self.lock_id} has no gateway or WiFi connectivity"
            )

        try:
            details = await self.api.get_lock(self.lock_id)

            new_data = deepcopy(self.data)

            # update mutable attributes
            new_data.name = details.name
            new_data.mac = details.mac
            new_data.model = details.model
            new_data.features = Features.from_feature_value(details.featureValue)
            new_data.battery_level = details.battery_level
            new_data.hardware_version = details.hardwareRevision
            new_data.firmware_version = details.firmwareRevision

            if Features.door_sensor in new_data.features:
                # make sure we have a placeholder for sensor state if the lock supports it
                if new_data.sensor is None:
                    new_data.sensor = SensorData()

                # only fetch sensor metadata once a day
                if (
                    new_data.sensor.last_fetched is None
                    or new_data.sensor.last_fetched < dt_util.now() - timedelta(days=1)
                ):
                    sensor = await self.api.get_sensor(self.lock_id)
                    new_data.sensor.last_fetched = dt_util.now()
                    if sensor:
                        new_data.sensor.battery = sensor.battery_level
            else:
                new_data.sensor = None

            try:
                state = await self.api.get_lock_state(self.lock_id)
                new_data.locked = state.locked == State.locked
                if sensor_present(new_data.sensor):
                    new_data.sensor.opened = state.opened == SensorState.opened
            except Exception as err:
                if new_data.locked is None:
                    # first-ever fetch: tolerate failure, entity shows "unknown"
                    _LOGGER.debug("Failed to fetch initial lock state", exc_info=True)
                else:
                    # we previously had a verified state - a failure to re-verify now
                    # means we can no longer trust it (e.g. gateway/webhooks silently
                    # died), so surface it via the standard unavailable path
                    raise UpdateFailed(
                        f"Failed to re-verify lock state for {self.lock_id}"
                    ) from err

            new_data.auto_lock_seconds = details.autoLockTime
            new_data.lock_sound = bool(details.lockSound)

            new_data.passage_mode_config = await self.api.get_lock_passage_mode_config(
                self.lock_id
            )
        except Exception as err:
            raise UpdateFailed(err) from err
        else:
            return new_data

    @callback
    def _async_refresh_finished(self) -> None:
        """Push refreshed device info back into the device registry.

        Entities snapshot `coordinator.device_info` once at add time, so once
        we start seeding placeholder data and filling it in progressively,
        this is what keeps the device registry's model/sw_version/hw_version
        in sync with later successful refreshes.
        """
        if self.last_update_success and self.config_entry is not None:
            dr.async_get(self.hass).async_get_or_create(
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
        """Device info for the lock."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.data.mac)},
            manufacturer="TT Lock",
            model=self.data.model,
            name=self.data.name,
            sw_version=self.data.firmware_version,
            hw_version=self.data.hardware_version,
        )

    @property
    def entities(self) -> list[Entity]:
        """Entities belonging to this co-ordinator."""
        result: list[Entity] = []
        for listener_callback, _ in list(self._listeners.values()):
            owner = listener_callback.__self__  # ty: ignore[unresolved-attribute] - checking for bound-method listeners
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
