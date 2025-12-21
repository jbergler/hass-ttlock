"""Support for iCloud sensors."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import lock_coordinators, sensor_present
from .entity import BaseLockEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    async_add_entities(
        [
            entity
            for coordinator in lock_coordinators(hass, entry)
            for entity in (
                PassageMode(coordinator),
                Sensor(coordinator)
                if sensor_present(coordinator.data.sensor)
                else None,
            )
            if entity is not None
        ]
    )


class Sensor(BaseLockEntity, BinarySensorEntity):
    """Current sensor state."""

    _attr_device_class = BinarySensorDeviceClass.DOOR

    def _update_from_coordinator(self) -> None:
        """Fetch state of device."""
        self._attr_name = f"{self.coordinator.data.name} Sensor"
        self._attr_is_on = (
            bool(self.coordinator.data.sensor.opened)
            if self.coordinator.data.sensor
            else False
        )


class PassageMode(BaseLockEntity, BinarySensorEntity):
    """Current passage mode state."""

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Passage Mode"
        self._attr_is_on = self.coordinator.data.passage_mode_active()
