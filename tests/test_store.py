"""Test the persisted per-lock LockStateStore."""

from custom_components.ttlock.store import LockStateStore


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
