"""Test the sensor platform's entity setup.

See coordinator.py's async_add_when_sensor_present: door-sensor presence
isn't known at platform-setup time - it's filled in by each lock's first
refresh, which __init__.py runs in the background after platforms are set up
(_fill_lock_details) - so SensorBattery has to be added once that refresh
confirms presence, not decided up front.
"""


async def test_sensor_battery_entity_appears_once_presence_confirmed(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("with_sensor")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("sensor.front_door_sensor_battery")
    assert state is not None
    assert state.state == "85"


async def test_sensor_battery_entity_not_created_when_absent(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("sensor_not_installed")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get("sensor.front_door_sensor_battery") is None


async def test_lock_battery_entity_created_immediately(
    hass, component_setup, mock_api_responses
):
    """Unlike SensorBattery, LockBattery doesn't depend on the deferred refresh."""
    mock_api_responses("default")
    await component_setup()

    assert hass.states.get("sensor.front_door_battery") is not None
