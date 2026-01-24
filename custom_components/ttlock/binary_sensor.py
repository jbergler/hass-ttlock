"""Support for iCloud sensors."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    GatewaysUpdateCoordinator,
    gateway_coordinator,
    lock_coordinators,
    sensor_present,
)
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

    coordinator = gateway_coordinator(hass, entry)
    async_add_entities(
        GatewaySensor(coordinator, gateway_id) for gateway_id in coordinator.data
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


class GatewaySensor(CoordinatorEntity[GatewaysUpdateCoordinator], BinarySensorEntity):
    """Gateway online status."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: GatewaysUpdateCoordinator, gateway_id: int) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.gateway_id = gateway_id
        self._attr_unique_id = f"{DOMAIN}-gateway-{gateway_id}"
        self._update_from_coordinator()

    @property
    def device_info(self) -> DeviceInfo:
        """Device info for the gateway."""
        gateway = self.coordinator.data[self.gateway_id]
        return DeviceInfo(
            identifiers={(DOMAIN, gateway.mac)},
            manufacturer="TT Lock",
            name=gateway.name,
            model="Gateway",
        )

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self.coordinator.data[self.gateway_id].is_online

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        gateway = self.coordinator.data[self.gateway_id]
        return {
            "network_name": gateway.network_name,
            "mac": gateway.mac,
        }

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        super()._handle_coordinator_update()

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        if self.gateway_id not in self.coordinator.data:
            return
        gateway = self.coordinator.data[self.gateway_id]
        self._attr_name = f"{gateway.name} Status"
