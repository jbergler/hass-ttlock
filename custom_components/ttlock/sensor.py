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

from .coordinator import (
    WIFI_LOCAL_STATES,
    async_add_when_sensor_present,
    lock_coordinators,
)
from .entity import (
    BaseLockEntity,
    LockDiagnosticEntity,
    local_admin_configured,
    wifi_local_configured,
)

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
                *([LockBleSignal(coordinator)] if coordinator.ble_enabled else []),
                LockConnection(coordinator),
                *(
                    [LockWifiLocalLink(coordinator)]
                    if wifi_local_configured(entry, coordinator)
                    else []
                ),
                *(
                    [LockCredentials(coordinator)]
                    if coordinator.access_level.can_admin
                    and (
                        coordinator.cloud_enabled
                        or local_admin_configured(entry, coordinator)
                    )
                    else []
                ),
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
    surfaced via device_info.via_device_id (coordinator.py); this is for
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


class LockBleSignal(BaseLockEntity, SensorEntity):
    """RSSI of the lock as heard directly by HA's own Bluetooth stack.

    Distinct from LockGateway, which reports how well the *TTLock gateway*
    hears the lock - this reports how well *we* hear it, with no TTLock
    hardware in between. Diagnostic and disabled by default: it exists to
    answer "is this lock within local radio range, and would a Bluetooth
    proxy help?", which is a placement question, not a dashboard one.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Bluetooth Signal"
        self._attr_native_value = self.coordinator.ble.rssi

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Which adapter/proxy heard the lock, when, and whether it can connect."""
        ble = self.coordinator.ble
        if not ble.in_range:
            return None
        return {
            "source": ble.source,
            "connectable": ble.connectable,
            "last_seen": ble.last_seen.isoformat() if ble.last_seen else None,
        }


class LockConnection(LockDiagnosticEntity, SensorEntity):
    """How the lock is currently reaching the integration.

    Value is the preferred transport that can reach the lock right now (BLE,
    wifi_local or cloud), by the configured transport order, or unknown if
    nothing can. Attributes explain whether a command would avoid the cloud and
    which transport last drove the bolt.
    """

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Connection"
        self._attr_native_value = self.coordinator.active_transport

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The full connectivity picture at a glance."""
        coordinator = self.coordinator
        return {
            "cloud_free": coordinator.control_cloud_free,
            "preferred_control_transport": coordinator.preferred_control_transport,
            "last_transport": coordinator.last_transport,
            "available_transports": coordinator.available_transports,
            "transport_order": coordinator.transport_order,
            "cloud_enabled": coordinator.cloud_enabled,
            "cloud_online": coordinator.cloud_online,
            "ble_reachable": coordinator.ble_reachable,
            "wifi_local": coordinator.wifi_local_state,
        }


class LockWifiLocalLink(LockDiagnosticEntity, SensorEntity):
    """State of the lock's wifi-local proxy link."""

    _attr_device_class = SensorDeviceClass.ENUM

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} WiFi-local Link"
        # Set here (not as a mutable class attribute) so the ENUM options are
        # declared without tripping ruff's RUF012 or ty's LSP check.
        self._attr_options = list(WIFI_LOCAL_STATES)
        self._attr_native_value = self.coordinator.wifi_local_state

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """When the link was last seen, alongside its state."""
        last_seen = self.coordinator.wifi_local_last_seen
        return {"last_seen": last_seen.isoformat() if last_seen else None}


class LockCredentials(LockDiagnosticEntity, SensorEntity):
    """How many passcodes/cards/fingerprints the lock has, with their names.

    The names come from the cloud and from credentials added on the lock from
    Home Assistant. The value is the total count; the names are attributes.
    Secret code digits are never fetched into or exposed by this entity.
    """

    _attr_entity_registry_enabled_default = False

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Credentials"
        counts = self.coordinator.credential_names.counts
        self._attr_native_value = sum(counts.values())

    @staticmethod
    def _as_list(kind: dict[int, Any]) -> list[dict[str, Any]]:
        """Render one id -> CredentialInfo map as a list of plain dicts."""
        return [
            {
                "id": cid,
                "name": info.name,
                "type": info.type,
                "valid_from": info.valid_from.isoformat() if info.valid_from else None,
                "valid_until": (
                    info.valid_until.isoformat() if info.valid_until else None
                ),
            }
            for cid, info in kind.items()
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Per-kind counts plus the names (never the secret digits)."""
        names = self.coordinator.credential_names
        counts = names.counts
        return {
            "passcode_count": counts["passcodes"],
            "card_count": counts["cards"],
            "fingerprint_count": counts["fingerprints"],
            "passcodes": self._as_list(names.passcodes),
            "cards": self._as_list(names.cards),
            "fingerprints": self._as_list(names.fingerprints),
        }
