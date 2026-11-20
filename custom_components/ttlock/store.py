"""Persisted state that must survive Home Assistant restarts.

Coordinator state (coordinator.py) is otherwise entirely in-memory and
recreated fresh on every restart. This holds what has to outlive that:

- per lock: the local credentials and the last lock/detail and passage-mode
  config, so a lock stays drivable over Bluetooth or WiFi-local while the
  TTLock cloud is unreachable or gone; and whether its door sensor has been
  confirmed absent, since TTLock's API can't distinguish "no sensor paired"
  from a transient failure (see LockUpdateCoordinator's door-sensor handling);
- per config entry: the account's key/list records, uid and gateway list, so
  setup can build the entry without the cloud.

Backed by a single `homeassistant.helpers.storage.Store`, shared across every
loaded config entry the same way LockTrafficCapture is (see __init__.py's
TT_STORE) - one JSON file for the whole install. Locks are keyed by lock ID
at the top level; config entries live under `_ENTRIES`, which a numeric lock
ID can never equal.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_lock_state"

_ENTRIES = "entries"


class LockStateStore:
    """Small persisted key/value store: one dict per lock ID and per entry."""

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
        """Merge `attributes` into a lock's persisted entry, saving if it changed."""
        await self._async_merge(await self._async_data(), str(lock_id), attributes)

    async def async_get_entry(self, entry_id: str) -> dict[str, Any]:
        """Return the persisted attributes for a config entry, or {} if none yet."""
        data = await self._async_data()
        return data.get(_ENTRIES, {}).get(entry_id, {})

    async def async_update_entry(self, entry_id: str, **attributes: Any) -> None:
        """Merge `attributes` into a config entry's persisted entry, saving if it changed.

        New non-empty `key_records` (a successful key/list) also forget every
        lock no persisted entry lists any more, secrets included.
        """
        data = await self._async_data()
        await self._async_merge(data.setdefault(_ENTRIES, {}), entry_id, attributes)
        if attributes.get("key_records") and self._prune_unlisted_locks(data):
            await self._store.async_save(data)

    @staticmethod
    def _prune_unlisted_locks(data: dict[str, Any]) -> bool:
        """Drop every lock no entry's key records list; whether any was dropped."""
        listed = {
            str(record["lockId"])
            for entry in data.get(_ENTRIES, {}).values()
            for record in entry.get("key_records", [])
        }
        unlisted = [key for key in data if key != _ENTRIES and key not in listed]
        for key in unlisted:
            del data[key]
        return bool(unlisted)

    async def _async_merge(
        self, section: dict[str, Any], key: str, attributes: dict[str, Any]
    ) -> None:
        merged = {**section.get(key, {}), **attributes}
        if merged == section.get(key):
            return
        section[key] = merged
        await self._store.async_save(await self._async_data())

    async def async_remove_entry(self, entry_id: str) -> None:
        """Forget a config entry and every lock no other entry lists, secrets included.

        A lock still listed in another entry's key records is kept for that entry.
        """
        data = await self._async_data()
        data.get(_ENTRIES, {}).pop(entry_id, None)
        self._prune_unlisted_locks(data)
        await self._store.async_save(data)
