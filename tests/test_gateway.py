"""Test the TTLock Gateway Binary Sensor."""

from unittest.mock import patch

import pytest

from custom_components.ttlock.const import DOMAIN, TT_GATEWAYS
from custom_components.ttlock.models import Gateway
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.core import HomeAssistant

GATEWAY_DETAILS = {
    "gatewayId": 1461158,
    "gatewayName": "Test Gateway",
    "gatewayMac": "05:F6:1E:93:8B:2B",
    "isOnline": 1,
    "networkName": "Battat2",
}

@pytest.fixture
def mock_gateway_response(monkeypatch):
    """Mock the get_gateways API response."""
    async def mock_get_gateways(*args, **kwargs):
        return [Gateway.parse_obj(GATEWAY_DETAILS)]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_gateways", mock_get_gateways
    )

async def test_gateway_sensor_setup(
    hass: HomeAssistant, component_setup, mock_api_responses, mock_gateway_response
):
    """Test that the gateway binary sensor is set up correctly."""
    mock_api_responses("default")
    await component_setup()

    # Get the gateway coordinator from hass.data to verify it's there
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    
    # Check if coordinator is stored
    assert TT_GATEWAYS in hass.data[DOMAIN][entry.entry_id]
    
    # Verify entity state
    # Entity ID should be binary_sensor.test_gateway_status (based on slugified name + status)
    # Actually, the name logic in implementation was f"{gateway.name} Status"
    # So it should be binary_sensor.test_gateway_status
    entity_id = "binary_sensor.test_gateway_status"
    state = hass.states.get(entity_id)
    
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["network_name"] == "Battat2"
    assert state.attributes["mac"] == "05:F6:1E:93:8B:2B"
    assert state.attributes["device_class"] == "connectivity"

async def test_gateway_sensor_offline(
    hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
):
    """Test that the gateway binary sensor reports offline."""
    offline_gateway = GATEWAY_DETAILS.copy()
    offline_gateway["isOnline"] = 0
    
    async def mock_get_gateways_offline(*args, **kwargs):
        return [Gateway.parse_obj(offline_gateway)]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_gateways", mock_get_gateways_offline
    )
    
    mock_api_responses("default")
    await component_setup()

    entity_id = "binary_sensor.test_gateway_status"
    state = hass.states.get(entity_id)
    
    assert state is not None
    assert state.state == STATE_OFF

async def test_setup_with_no_gateways(
    hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
):
    """Test setup when account has no gateways."""
    async def mock_get_gateways_empty(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_gateways", mock_get_gateways_empty
    )
    
    mock_api_responses("default")
    await component_setup()

    # Verify coordinator exists but has empty data
    entries = hass.config_entries.async_entries(DOMAIN)
    entry = entries[0]
    assert TT_GATEWAYS in hass.data[DOMAIN][entry.entry_id]
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_GATEWAYS]
    assert coordinator.data == {}
    
    # Verify no gateway entities created
    # We can check specific naming pattern or simply that no binary_sensor.gateway* exists
    states = hass.states.async_all()
    gateway_sensors = [
        state.entity_id for state in states 
        if state.entity_id.startswith("binary_sensor.") and "gateway" in state.entity_id
    ]
    assert len(gateway_sensors) == 0
