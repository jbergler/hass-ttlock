"""Number platform: the auto-lock delay as an editable seconds value.

The auto-lock *switch* (switch.py) answers "is auto-lock on?"; this number sets
the actual delay. Both drive `coordinator.set_auto_lock_seconds`, which tries
Bluetooth and the WiFi proxy before the cloud.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import AUTO_LOCK_MAX_SECONDS
from .coordinator import lock_coordinators
from .entity import BaseLockEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the admin-only auto-lock delay number.

    Skipped for MEMBER access, like the admin switches - setting the delay is an
    admin op on every transport.
    """
    async_add_entities(
        AutoLockDelay(coordinator)
        for coordinator in lock_coordinators(hass, entry)
        if coordinator.access_level.can_admin
    )


class AutoLockDelay(BaseLockEntity, NumberEntity):
    """The lock's auto-lock delay, in seconds (0 disables)."""

    # 0 disables auto-lock; it sits below the lock's own minimum delay.
    _attr_native_min_value = 0
    _attr_native_max_value = AUTO_LOCK_MAX_SECONDS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def _update_from_coordinator(self) -> None:
        """Reflect the cached delay."""
        self._attr_name = f"{self.coordinator.data.name} Auto Lock Delay"
        # None (never read) is unknown; 0 is auto-lock off.
        self._attr_native_value = self.coordinator.data.auto_lock_seconds

    async def async_set_native_value(self, value: float) -> None:
        """Set the auto-lock delay (0 disables)."""
        await self.coordinator.set_auto_lock_seconds(int(value))
