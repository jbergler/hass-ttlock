"""Support for iCloud sensors."""

from __future__ import annotations

import logging
from typing import Any

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
    door_state_known,
    gateway_coordinator,
    lock_coordinators,
)
from .entity import BaseLockEntity, LockDiagnosticEntity

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
                PassageMode(coordinator),
                LockCloudOnline(coordinator),
                *([LockBleReachable(coordinator)] if coordinator.ble_enabled else []),
            )
        ]
    )

    for lock_coordinator in coordinators:
        async_add_when_sensor_present(
            lock_coordinator,
            lambda lock_coordinator=lock_coordinator: async_add_entities(
                [Sensor(lock_coordinator)]
            ),
            ready=door_state_known,
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
        # A door nobody has read yet is unknown, not closed.
        self._attr_is_on = (
            self.coordinator.data.sensor.opened
            if self.coordinator.data.sensor
            else None
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


class _ConnectivityBinary(LockDiagnosticEntity, BinarySensorEntity):
    """Base for the connectivity binary sensors."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY


class LockBleReachable(_ConnectivityBinary):
    """Whether HA's own Bluetooth can currently reach the lock."""

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Bluetooth Reachable"
        self._attr_is_on = self.coordinator.ble_reachable

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The locally-heard RSSI, when the lock is in range."""
        ble = self.coordinator.ble
        return {"rssi": ble.rssi if ble.in_range else None}


class LockCloudOnline(_ConnectivityBinary):
    """Whether the TTLock cloud can currently reach the lock."""

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Cloud Online"
        self._attr_is_on = self.coordinator.cloud_online
