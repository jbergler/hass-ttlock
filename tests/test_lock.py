from unittest.mock import AsyncMock

import pytest

from custom_components.ttlock.coordinator import lock_action
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError

from .const import (
    BASIC_EKEY_RECORD_MEMBER,
    BASIC_EKEY_RECORD_MEMBER_FROZEN,
    BASIC_EKEY_RECORD_MEMBER_NO_REMOTE,
    BASIC_EKEY_RECORD_OWNER,
)


async def test_lock_states(hass: HomeAssistant, mock_api_responses, component_setup):
    """Test different lock states."""
    mock_api_responses("default")
    coordinator = await component_setup()
    assert not coordinator.data.locked


async def test_an_unknown_lock_claims_no_direction(
    hass: HomeAssistant, mock_api_responses, component_setup
):
    """Locking or unlocking is only knowable from where the bolt was.

    A lock we could not read stays unknown for the duration of the action
    instead of reporting a direction it can't know.
    """
    mock_api_responses("state_unknown")
    coordinator = await component_setup()
    assert coordinator.data.locked is None

    with lock_action(coordinator):
        state = hass.states.get("lock.front_door")
        assert state is not None
        assert state.state == STATE_UNKNOWN


async def test_lock_restores_last_known_state_before_first_poll(
    hass: HomeAssistant, mock_api_responses, component_setup, monkeypatch
):
    """Lock restores locked/unlocked from RestoreEntity while coordinator.data.locked is None."""

    async def _mock_last_state(self):
        return State("lock.front_door", "locked")

    mock_api_responses("state_unknown")
    monkeypatch.setattr(
        "homeassistant.helpers.restore_state.RestoreEntity.async_get_last_state",
        _mock_last_state,
    )
    coordinator = await component_setup()
    assert coordinator.data.locked is None

    coordinator.data.locked = None
    lock_entity = next(
        e for e in coordinator.entities if e.entity_id == "lock.front_door"
    )
    await lock_entity.async_added_to_hass()
    assert coordinator.data.locked is True


def _lock_entity(coordinator):
    return next(
        entity
        for entity in coordinator.entities
        if entity.entity_id.startswith("lock.")
    )


@pytest.mark.parametrize(
    ("record", "entity_method", "api_method"),
    [
        (BASIC_EKEY_RECORD_OWNER, "async_lock", "lock"),
        (BASIC_EKEY_RECORD_MEMBER, "async_unlock", "unlock"),
    ],
)
async def test_owner_and_remote_enabled_member_can_drive_lock(
    ekey_component_setup, monkeypatch, record, entity_method, api_method
):
    """Owned and permitted shared locks remain controllable from HA."""
    coordinator = await ekey_component_setup(record)
    request = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, api_method, request)

    await getattr(_lock_entity(coordinator), entity_method)()

    request.assert_awaited_once_with(coordinator.lock_id)


@pytest.mark.parametrize(
    ("record", "translation_key"),
    [
        (BASIC_EKEY_RECORD_MEMBER_NO_REMOTE, "shared_lock_no_remote_unlock"),
        (BASIC_EKEY_RECORD_MEMBER_FROZEN, "shared_lock_frozen"),
        (
            {**BASIC_EKEY_RECORD_MEMBER, "startDate": 4102444800000},
            "shared_lock_not_yet_valid",
        ),
        (
            {**BASIC_EKEY_RECORD_MEMBER, "endDate": 1},
            "shared_lock_expired",
        ),
    ],
)
async def test_disallowed_shared_ekey_cannot_unlock(
    ekey_component_setup, monkeypatch, record, translation_key
):
    """HA rejects an unusable shared ekey before sending a cloud request."""
    coordinator = await ekey_component_setup(record)
    request = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "unlock", request)

    with pytest.raises(ServiceValidationError) as exc_info:
        await _lock_entity(coordinator).async_unlock()

    assert exc_info.value.translation_key == translation_key
    request.assert_not_awaited()


async def test_shared_ekey_metadata_is_exposed_in_entity_state(
    hass: HomeAssistant, ekey_component_setup
):
    """Automations can inspect access, status, validity and remote permission."""
    coordinator = await ekey_component_setup(
        {
            **BASIC_EKEY_RECORD_MEMBER,
            "startDate": 1893456000000,
            "endDate": 1893542400000,
        }
    )

    state = hass.states.get(_lock_entity(coordinator).entity_id)
    assert state is not None
    assert state.attributes["access_level"] == "member"
    assert state.attributes["key_status"] == "110401"
    assert state.attributes["valid_from"] == 1893456000000
    assert state.attributes["valid_until"] == 1893542400000
    assert state.attributes["remote_unlock_enabled"] is True
    assert not any(
        entity.entity_id.startswith("switch.") for entity in coordinator.entities
    )
