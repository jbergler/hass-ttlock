"""The actual lock part of the locks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import lock_coordinators
from .entity import BaseLockEntity
from .models import AccessLevel


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    async_add_entities(
        [Lock(coordinator) for coordinator in lock_coordinators(hass, entry)]
    )


class Lock(BaseLockEntity, LockEntity, RestoreEntity):
    """The entity object for a lock."""

    async def async_added_to_hass(self) -> None:
        """Restore last known lock state before the background poll completes."""
        await super().async_added_to_hass()
        if self.coordinator.data.locked is None:
            if (last_state := await self.async_get_last_state()) is not None:
                if last_state.state in ("locked", "unlocked"):
                    self.coordinator.data.locked = last_state.state == "locked"
                    self._update_from_coordinator()
                    self.async_write_ha_state()

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = self.coordinator.data.name

        self._attr_is_locked = self.coordinator.data.locked
        # Which way the bolt is moving is only knowable from where it was, so
        # an action on a lock we couldn't read leaves the state unknown
        # rather than claiming a direction.
        pending = (
            self.coordinator.data.action_pending and self._attr_is_locked is not None
        )
        self._attr_is_locking = pending and not self._attr_is_locked
        self._attr_is_unlocking = pending and self._attr_is_locked

    async def async_lock(self, **kwargs: Any) -> None:
        """Try to lock the lock, unless the ekey disallows it."""
        self._ensure_remote_unlock_allowed()
        await self.coordinator.lock()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Try to unlock the lock, unless the ekey disallows it."""
        self._ensure_remote_unlock_allowed()
        await self.coordinator.unlock()

    def _ensure_remote_unlock_allowed(self) -> None:
        """Raise if the ekey disallows this account from driving the lock.

        OWNER always may. Shared ekeys honour validity window, freeze
        state, and remoteEnable - each with its own translation key.
        """
        if self.coordinator.access_level is AccessLevel.OWNER:
            return
        summary = self.coordinator.summary
        now_ms = int(dt_util.utcnow().timestamp() * 1000)
        valid, reason = summary.is_valid_now(now_ms)
        if not valid:
            assert reason is not None
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=reason,
            )
        if not summary.remote_unlock_enabled:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="shared_lock_no_remote_unlock",
            )
