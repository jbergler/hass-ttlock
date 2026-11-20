"""Test the persisted per-lock LockStateStore."""

from unittest.mock import patch

from custom_components.ttlock.store import LockStateStore
from homeassistant.helpers.storage import Store


async def test_get_returns_empty_dict_for_unknown_lock(hass):
    store = LockStateStore(hass)
    assert await store.async_get(123) == {}


async def test_update_then_get_round_trips(hass):
    store = LockStateStore(hass)
    await store.async_update(123, foo="bar", count=1)
    assert await store.async_get(123) == {"foo": "bar", "count": 1}


async def test_update_merges_rather_than_replaces(hass):
    store = LockStateStore(hass)
    await store.async_update(123, foo="bar")
    await store.async_update(123, count=1)
    assert await store.async_get(123) == {"foo": "bar", "count": 1}


async def test_locks_are_independent(hass):
    store = LockStateStore(hass)
    await store.async_update(123, foo="bar")
    await store.async_update(456, foo="baz")
    assert await store.async_get(123) == {"foo": "bar"}
    assert await store.async_get(456) == {"foo": "baz"}


async def test_state_persists_across_store_instances(hass):
    """A second LockStateStore(hass) - as happens on HA restart - must see
    what a previous instance saved, since the whole point is surviving
    restarts.
    """
    await LockStateStore(hass).async_update(123, foo="bar")
    assert await LockStateStore(hass).async_get(123) == {"foo": "bar"}


async def test_entries_are_kept_apart_from_locks(hass):
    store = LockStateStore(hass)
    await store.async_update(123, foo="bar")
    await store.async_update_entry("entry-a", uid=7)
    await store.async_update_entry("entry-a", key_records=[])
    assert await store.async_get_entry("entry-a") == {"uid": 7, "key_records": []}
    assert await store.async_get_entry("entry-b") == {}
    assert await LockStateStore(hass).async_get(123) == {"foo": "bar"}


async def test_an_unchanged_update_is_not_saved(hass):
    store = LockStateStore(hass)
    await store.async_update(123, foo="bar")
    with patch.object(Store, "async_save") as save:
        await store.async_update(123, foo="bar")
        await store.async_update_entry("entry-a")
        await store.async_update_entry("entry-a")
    assert save.await_count == 1  # only the entry's first appearance


async def test_removing_an_entry_purges_only_its_own_locks(hass):
    store = LockStateStore(hass)
    await store.async_update(1, ble_key="secret-1")
    await store.async_update(2, ble_key="secret-2")
    await store.async_update_entry(
        "entry-a", key_records=[{"lockId": 1}, {"lockId": 2}]
    )
    await store.async_update_entry("entry-b", key_records=[{"lockId": 2}])

    await store.async_remove_entry("entry-a")

    fresh = LockStateStore(hass)
    assert await fresh.async_get_entry("entry-a") == {}
    assert await fresh.async_get(1) == {}
    assert await fresh.async_get(2) == {"ble_key": "secret-2"}  # entry-b's lock


async def test_an_enumeration_prunes_locks_no_entry_lists_any_more(hass):
    """A lock dropped from every entry's key list loses its cached secrets."""
    store = LockStateStore(hass)
    # Each entry's records are stored before its locks' state, as setup does.
    await store.async_update_entry(
        "entry-a", key_records=[{"lockId": 1}, {"lockId": 3}]
    )
    await store.async_update_entry("entry-b", key_records=[{"lockId": 2}])
    await store.async_update(1, ble_key="secret-1")
    await store.async_update(2, ble_key="secret-2")
    await store.async_update(3, ble_key="secret-3")

    # Lock 3 was removed from the account: entry-a's next key/list lacks it.
    await store.async_update_entry("entry-a", key_records=[{"lockId": 1}])

    fresh = LockStateStore(hass)
    assert await fresh.async_get(1) == {"ble_key": "secret-1"}
    assert await fresh.async_get(2) == {"ble_key": "secret-2"}
    assert await fresh.async_get(3) == {}


async def test_removing_an_entry_also_prunes_orphaned_locks(hass):
    store = LockStateStore(hass)
    await store.async_update(1, ble_key="secret-1")
    await store.async_update(9, ble_key="orphan")  # listed by no entry
    await store.async_update_entry("entry-a", key_records=[{"lockId": 1}])
    await store.async_update_entry("entry-b", key_records=[])

    await store.async_remove_entry("entry-b")

    fresh = LockStateStore(hass)
    assert await fresh.async_get(9) == {}
    assert await fresh.async_get(1) == {"ble_key": "secret-1"}
