"""Tests for the auto-lock delay Number entity."""

from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from .test_services import answer_locally


def _number_entity(coordinator):
    return next(
        entity
        for entity in coordinator.entities
        if entity.entity_id.startswith("number.")
    )


class TestAutoLockDelay:
    async def test_entity_is_created_and_reflects_the_delay(
        self, hass, component_setup, mock_api_responses
    ):
        mock_api_responses("default")
        coordinator = await component_setup()
        coordinator.data.auto_lock_seconds = 30
        number = _number_entity(coordinator)
        number._update_from_coordinator()  # noqa: SLF001 - refresh cached value

        assert number.native_value == 30
        assert number.native_unit_of_measurement == "s"

    @pytest.mark.parametrize(("cached", "shown"), [(None, None), (0, 0)])
    async def test_an_unknown_delay_is_unknown_not_zero(
        self, hass, component_setup, mock_api_responses, cached, shown
    ):
        """0 means auto-lock off; a delay never read must not claim that."""
        mock_api_responses("default")
        coordinator = await component_setup()
        coordinator.data.auto_lock_seconds = cached
        number = _number_entity(coordinator)
        number._update_from_coordinator()  # noqa: SLF001 - refresh cached value

        assert number.native_value == shown

    async def test_setting_the_value_drives_the_coordinator(
        self, hass, component_setup, mock_api_responses
    ):
        mock_api_responses("default")
        coordinator = await component_setup()
        number = _number_entity(coordinator)
        coordinator.set_auto_lock_seconds = AsyncMock()

        await number.async_set_native_value(45)

        coordinator.set_auto_lock_seconds.assert_awaited_once_with(45)

    async def test_unique_id_does_not_clash_with_the_switch(
        self, hass, component_setup, mock_api_responses
    ):
        mock_api_responses("default")
        coordinator = await component_setup()
        number = _number_entity(coordinator)
        switch = next(
            entity
            for entity in coordinator.entities
            if entity.entity_id.startswith("switch.")
            and "auto" in entity.entity_id.lower()
        )

        assert number.unique_id != switch.unique_id


class TestAutoLockDelayErrors:
    async def test_a_lock_refusal_reaches_the_user_translated(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        mock_api_responses("default")
        coordinator = await component_setup()
        number = _number_entity(coordinator)
        answer_locally(coordinator, monkeypatch, {0x36: [bytes([0x36, 0x00])]})

        with pytest.raises(HomeAssistantError) as excinfo:
            await number.async_set_native_value(5000)

        assert excinfo.value.translation_key == "local_command_rejected"
