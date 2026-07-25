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
    async_add_when_sensor_present,
    gateway_coordinator,
    lock_coordinators,
)
from .entity import BaseLockEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    coordinators = list(lock_coordinators(hass, entry))

    async_add_entities([PassageMode(coordinator) for coordinator in coordinators])

    for lock_coordinator in coordinators:
        async_add_when_sensor_present(
            lock_coordinator,
            lambda lock_coordinator=lock_coordinator: async_add_entities(
                [Sensor(lock_coordinator)]
            ),
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
    def available(self) -> bool:
        """Return whether the gateway is still known to the coordinator."""
        return super().available and self.gateway_id in self.coordinator.data

    @property
    def device_info(self) -> DeviceInfo | None:
        """Device info for the gateway."""
        gateway = self.coordinator.data.get(self.gateway_id)
        if gateway is None:
            return None
        return DeviceInfo(
            identifiers={(DOMAIN, gateway.mac)},
            manufacturer="TT Lock",
            name=gateway.name,
            model="Gateway",
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        gateway = self.coordinator.data.get(self.gateway_id)
        return gateway.is_online if gateway else None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        gateway = self.coordinator.data.get(self.gateway_id)
        if gateway is None:
            return None
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
        gateway = self.coordinator.data.get(self.gateway_id)
        if gateway is not None:
            self._attr_name = f"{gateway.name} Status"
