"""Persisted per-lock state that must survive Home Assistant restarts.

Coordinator state (coordinator.py) is otherwise entirely in-memory and
recreated fresh on every restart. This exists for the rare bit of state that
needs to survive that - e.g. whether a lock's door sensor has been confirmed
absent, since TTLock's API can't distinguish "no sensor paired" from a
transient failure (see LockUpdateCoordinator's door-sensor handling), so
re-guessing from scratch on every restart would defeat the point of giving
up.

Backed by a single `homeassistant.helpers.storage.Store`, keyed by lock ID,
shared across every loaded config entry the same way LockTrafficCapture is
(see __init__.py's _STORE_KEY) - one JSON file for the whole install, not one
per account.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_lock_state"


class LockStateStore:
    """Small persisted key/value store, one dict of attributes per lock ID."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize, wrapping a Store that's only actually read on first use."""
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._data: dict[str, dict[str, Any]] | None = None

    async def _async_data(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            self._data = await self._store.async_load() or {}
        return self._data

    async def async_get(self, lock_id: int) -> dict[str, Any]:
        """Return the persisted attributes for a lock, or {} if none yet."""
        data = await self._async_data()
        return data.get(str(lock_id), {})

    async def async_update(self, lock_id: int, **attributes: Any) -> None:
        """Merge `attributes` into a lock's persisted entry and save."""
        data = await self._async_data()
        data[str(lock_id)] = {**data.get(str(lock_id), {}), **attributes}
        await self._store.async_save(data)
