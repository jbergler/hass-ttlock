"""Test the TTLock debug capture button."""

from datetime import timedelta
import logging

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ttlock.const import get_device_logger
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util


async def test_press_starts_debug_capture_and_reverts_after_window(
    hass: HomeAssistant, mock_api_responses, component_setup
):
    """Pressing the button raises the lock's logger to debug, then reverts it after 30 minutes."""
    assert await async_setup_component(hass, "logger", {})

    mock_api_responses("default")
    coordinator = await component_setup()

    device_logger = get_device_logger(coordinator.lock_id)
    device_logger.setLevel(logging.WARNING)

    button_entity = next(
        entity
        for entity in coordinator.entities
        if entity.entity_id.startswith("button.")
    )

    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: button_entity.entity_id},
        blocking=True,
    )

    assert device_logger.level == logging.DEBUG

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()

    assert device_logger.level == logging.WARNING
