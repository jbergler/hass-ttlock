"""Test the binary_sensor platform's entity setup.

See coordinator.py's async_add_when_sensor_present: door-sensor presence
isn't known at platform-setup time - it's filled in by each lock's first
refresh, which __init__.py runs in the background after platforms are set up
(_fill_lock_details) - so the Sensor entity has to be added once that
refresh confirms presence, not decided up front.
"""

from homeassistant.const import STATE_OFF, STATE_UNKNOWN


async def test_sensor_entity_appears_once_presence_confirmed(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("with_sensor")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("binary_sensor.front_door_sensor")
    assert state is not None


async def test_sensor_reports_a_door_it_read(hass, component_setup, mock_api_responses):
    mock_api_responses("with_sensor")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("binary_sensor.front_door_sensor")
    assert state is not None
    assert state.state == STATE_OFF


async def test_sensor_unknown_until_the_door_is_read(
    hass, component_setup, mock_api_responses
):
    """A paired sensor whose door state hasn't been read isn't a closed door."""
    mock_api_responses("sensor_state_unknown")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("binary_sensor.front_door_sensor")
    assert state is not None
    assert state.state == STATE_UNKNOWN


async def test_sensor_entity_not_created_when_absent(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("sensor_not_installed")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get("binary_sensor.front_door_sensor") is None


async def test_passage_mode_entity_created_immediately(
    hass, component_setup, mock_api_responses
):
    """Unlike Sensor, PassageMode doesn't depend on the deferred refresh."""
    mock_api_responses("default")
    await component_setup()

    assert hass.states.get("binary_sensor.front_door_passage_mode") is not None
