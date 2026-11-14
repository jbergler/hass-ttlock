"""Base entity class for TTLock integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import NOT_CONNECTABLE_REASON, LockUpdateCoordinator
from .models import AccessLevel


class BaseLockEntity(CoordinatorEntity[LockUpdateCoordinator], ABC):
    """Abstract base class for lock entity."""

    coordinator: LockUpdateCoordinator

    def __init__(
        self,
        coordinator: LockUpdateCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = (
            f"{coordinator.unique_id}-{self.__class__.__name__.lower()}"
        )
        self._update_from_coordinator()

        # self.entity_description = description

    @abstractmethod
    def _update_from_coordinator(self) -> None:
        pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Unavailable if non-connectable, or periodic re-verification is failing."""
        return self.coordinator.connectable and super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Explain availability + surface ekey metadata when present."""
        attrs: dict[str, Any] = {}
        if not self.coordinator.connectable:
            attrs["unavailable_reason"] = NOT_CONNECTABLE_REASON
        # Shared locks (and the OWNER's own ekey record, for symmetry) carry
        # the access_level and any validity window / remote-unlock flag the
        # admin configured. Automations can branch on this; diagnostics can
        # explain why an admin-only service was rejected.
        attrs["access_level"] = self.coordinator.access_level.value
        if self.coordinator.ekey_key_status is not None:
            attrs["key_status"] = self.coordinator.ekey_key_status
        if self.coordinator.ekey_start_date:
            attrs["valid_from"] = self.coordinator.ekey_start_date
        if self.coordinator.ekey_end_date:
            attrs["valid_until"] = self.coordinator.ekey_end_date
        if self.coordinator.access_level is not AccessLevel.OWNER:
            # Only meaningful on shared ekeys - OWNER always has full control
            # via admin REST paths.
            attrs["remote_unlock_enabled"] = self.coordinator.ekey_remote_unlock
        return attrs
