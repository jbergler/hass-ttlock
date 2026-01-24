"""Test the TTLock Gateway Binary Sensor."""

from unittest.mock import patch

import pytest

from custom_components.ttlock.const import DOMAIN, TT_GATEWAYS
from custom_components.ttlock.models import Gateway
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.core import HomeAssistant

GATEWAY_DETAILS = {
    "deviceNum": 1,
    "serialNumber": "G2_2b8b93",
    "lockNum": 1,
    "plugName": "Test Gateway",
    "electricMeterCount": 0,
    "plugMac": "05:F6:1E:93:8B:2B",
    "networkName": "Battat2",
    "waterMeterCount": 0,
    "isOnline": 1,
    "plugVersion": 2,
    "plugId": 1461158,
    "networkMac": "cc:7b:5c:4f:b7:17",
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
