"""Support for iCloud sensors."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import async_add_when_sensor_present, lock_coordinators
from .entity import BaseLockEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    coordinators = list(lock_coordinators(hass, entry))

    async_add_entities(
        [
            entity
            for coordinator in coordinators
            for entity in (
                LockBattery(coordinator),
                LockOperator(coordinator),
                LockTrigger(coordinator),
                *([LockGateway(coordinator)] if coordinator.has_gateway else []),
            )
        ]
    )

    for lock_coordinator in coordinators:
        async_add_when_sensor_present(
            lock_coordinator,
            lambda lock_coordinator=lock_coordinator: async_add_entities(
                [SensorBattery(lock_coordinator)]
            ),
        )


class LockBattery(BaseLockEntity, SensorEntity):
    """Representation of a locks battery state."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Battery"
        self._attr_native_value = self.coordinator.data.battery_level


class LockOperator(BaseLockEntity, RestoreEntity, SensorEntity):
    """Representation of a locks last operator."""

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Last Operator"
        if self.coordinator.data.last_user:
            self._attr_native_value = self.coordinator.data.last_user
        elif not self._attr_native_value:
            self._attr_native_value = "Unknown"

    async def async_added_to_hass(self) -> None:
        """Restore on startup since we don't have event history."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if not last_state or last_state.state == STATE_UNAVAILABLE:
            return

        self._attr_native_value = last_state.state


class LockTrigger(BaseLockEntity, RestoreEntity, SensorEntity):
    """Representation of a locks state change reason."""

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Last Trigger"
        if self.coordinator.data.last_reason:
            self._attr_native_value = self.coordinator.data.last_reason
        elif not self._attr_native_value:
            self._attr_native_value = "Unknown"

    async def async_added_to_hass(self) -> None:
        """Restore on startup since we don't have event history."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if not last_state or last_state.state == STATE_UNAVAILABLE:
            return

        self._attr_native_value = last_state.state


class SensorBattery(BaseLockEntity, SensorEntity):
    """Representation of sensor battery."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Sensor Battery"
        self._attr_native_value = (
            self.coordinator.data.sensor.battery
            if self.coordinator.data.sensor
            else None
        )


class LockGateway(BaseLockEntity, SensorEntity):
    """RSSI of the gateway currently used to reach the lock.

    Diagnostic and disabled by default - the same connectivity is already
    surfaced via device_info.via_device (coordinator.py); this is for
    troubleshooting placement/coverage, not everyday use.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Gateway Signal"
        best = self.coordinator.data.best_gateway
        self._attr_native_value = best.rssi if best else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Name/mac of the connected gateway, plus any others in range."""
        gateways = self.coordinator.data.gateways
        if not gateways:
            return None
        best, *others = gateways
        return {
            "gateway": best.name,
            "mac": best.mac,
            "other_gateways": [
                {"name": gateway.name, "mac": gateway.mac, "rssi": gateway.rssi}
                for gateway in others
            ],
        }
