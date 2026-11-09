from custom_components.ttlock.coordinator import lock_action
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State


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
