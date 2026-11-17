"""Tests for the lock's admin switches.

`_switches_for` decides which switches a lock exposes. Auto-lock and lock-sound
are always present for an admin. Tamper alert and passage auto-unlock have no
cloud setter, so they need a local transport and the lock to report the
feature; a lock without either never gets an always-failing switch.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import CONF_WIFI_LOCAL_ENABLED, DOMAIN
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import Features, LockSummary
from custom_components.ttlock.store import LockStateStore
from custom_components.ttlock.switch import (
    AutoLock,
    LockSound,
    PassageAutoUnlock,
    TamperAlert,
    _switches_for,
)
from homeassistant.exceptions import HomeAssistantError

from .const import BASIC_EKEY_RECORD_MEMBER
from .test_services import answer_locally


def _coordinator(hass, api, features: Features) -> LockUpdateCoordinator:
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    summary = LockSummary(
        lockId=1,
        lockAlias="Front door",
        lockMac="00:00:00:00:00:00",
        hasGateway=0,
        userType="110301",
    )
    coordinator = LockUpdateCoordinator(
        hass,
        config_entry,
        api,
        summary,
        LockTrafficCapture(),
        LockStateStore(hass),
    )
    coordinator.data.features = features
    coordinator.async_update_listeners = Mock()
    return coordinator


class TestSwitchGating:
    def test_base_switches_always_present(self, hass, api):
        switches = _switches_for(_coordinator(hass, api, Features(0)), local_admin=True)
        kinds = {type(s) for s in switches}
        assert AutoLock in kinds
        assert LockSound in kinds
        assert TamperAlert not in kinds
        assert PassageAutoUnlock not in kinds

    def test_tamper_added_when_supported(self, hass, api):
        switches = _switches_for(
            _coordinator(hass, api, Features.tamper_alert), local_admin=True
        )
        assert any(isinstance(s, TamperAlert) for s in switches)

    def test_passage_auto_unlock_added_when_passage_supported(self, hass, api):
        switches = _switches_for(
            _coordinator(hass, api, Features.passage_mode), local_admin=True
        )
        assert any(isinstance(s, PassageAutoUnlock) for s in switches)

    def test_both_when_both_supported(self, hass, api):
        features = Features.tamper_alert | Features.passage_mode
        switches = _switches_for(_coordinator(hass, api, features), local_admin=True)
        kinds = {type(s) for s in switches}
        assert {TamperAlert, PassageAutoUnlock} <= kinds

    def test_local_only_switches_need_a_local_transport(self, hass, api):
        features = Features.tamper_alert | Features.passage_mode
        switches = _switches_for(_coordinator(hass, api, features), local_admin=False)
        assert {type(s) for s in switches} == {AutoLock, LockSound}


class TestTamperSwitch:
    async def test_turn_on_calls_coordinator(self, hass, api):
        coordinator = _coordinator(hass, api, Features.tamper_alert)
        set_tamper = AsyncMock()
        coordinator.set_tamper_alert = set_tamper
        entity = TamperAlert(coordinator)

        await entity.async_turn_on()

        set_tamper.assert_awaited_once_with(True)

    async def test_turn_off_calls_coordinator(self, hass, api):
        coordinator = _coordinator(hass, api, Features.tamper_alert)
        set_tamper = AsyncMock()
        coordinator.set_tamper_alert = set_tamper
        entity = TamperAlert(coordinator)

        await entity.async_turn_off()

        set_tamper.assert_awaited_once_with(False)

    async def test_a_lock_refusal_reaches_the_user_translated(
        self, hass, api, monkeypatch
    ):
        coordinator = _coordinator(hass, api, Features.tamper_alert)
        answer_locally(coordinator, monkeypatch, {0x68: bytes([0x68, 0x00])})

        with pytest.raises(HomeAssistantError) as excinfo:
            await TamperAlert(coordinator).async_turn_on()

        assert excinfo.value.translation_key == "local_command_rejected"

    def test_state_from_coordinator(self, hass, api):
        coordinator = _coordinator(hass, api, Features.tamper_alert)
        coordinator.data.tamper_alert = True
        # The base entity reads the coordinator in __init__.
        entity = TamperAlert(coordinator)
        assert entity.is_on is True


class TestUnknownSettings:
    @pytest.mark.parametrize(
        ("seconds", "shown"), [(None, None), (0, False), (-1, False), (30, True)]
    )
    def test_auto_lock_unknown_delay_is_unknown_not_off(
        self, hass, api, seconds, shown
    ):
        coordinator = _coordinator(hass, api, Features(0))
        coordinator.data.auto_lock_seconds = seconds
        assert AutoLock(coordinator).is_on is shown

    @pytest.mark.parametrize(
        "entity_class", [LockSound, TamperAlert, PassageAutoUnlock]
    )
    def test_an_unread_switch_is_unknown(self, hass, api, entity_class):
        coordinator = _coordinator(hass, api, Features(0))
        assert entity_class(coordinator).is_on is None


class TestPassageAutoUnlockSwitch:
    async def test_turn_on_calls_coordinator(self, hass, api):
        coordinator = _coordinator(hass, api, Features.passage_mode)
        set_auto = AsyncMock()
        coordinator.set_passage_auto_unlock = set_auto
        entity = PassageAutoUnlock(coordinator)

        await entity.async_turn_on()

        set_auto.assert_awaited_once_with(True)

    async def test_turn_off_calls_coordinator(self, hass, api):
        coordinator = _coordinator(hass, api, Features.passage_mode)
        set_auto = AsyncMock()
        coordinator.set_passage_auto_unlock = set_auto
        entity = PassageAutoUnlock(coordinator)

        await entity.async_turn_off()

        set_auto.assert_awaited_once_with(False)


def _switch_names(hass) -> list[str]:
    return [state.name for state in hass.states.async_all("switch")]


class TestSwitchPlatformSetup:
    # The default mock lock's featureValue (F44354CD5F3) has bit 28 (tamper)
    # and bit 22 (passage).

    async def test_local_switches_register_with_the_wifi_proxy_on(
        self, hass, multi_account_credential, new_mocked_entry, mock_api_responses
    ):
        mock_api_responses("default")
        await multi_account_credential(hass)
        wifi_lock = LockSummary(
            lockId=7252408,
            lockAlias="Front Door",
            lockMac="00:00:00:00:00:01",
            # WiFi (bit 56), tamper (bit 28) and passage (bit 22).
            featureValue=f"{2**56 | 2**28 | 2**22:X}",
            userType="110301",
        )
        entry = new_mocked_entry(options={CONF_WIFI_LOCAL_ENABLED: True})
        entry.add_to_hass(hass)
        with patch(
            "custom_components.ttlock.api.TTLockApi.get_locks",
            AsyncMock(return_value=[wifi_lock]),
        ):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
        names = _switch_names(hass)
        assert any("Tamper Alert" in name for name in names)
        assert any("Passage Auto Unlock" in name for name in names)

    async def test_local_switches_register_with_bluetooth_control_on(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        mock_api_responses("default")
        monkeypatch.setattr(
            LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
        )
        await component_setup()
        names = _switch_names(hass)
        assert any("Tamper Alert" in name for name in names)
        assert any("Passage Auto Unlock" in name for name in names)

    async def test_no_local_switches_without_a_local_transport(
        self, hass, component_setup, mock_api_responses
    ):
        mock_api_responses("default")
        await component_setup()
        names = _switch_names(hass)
        assert any("Auto Lock" in name for name in names)
        assert not any("Tamper Alert" in name for name in names)
        assert not any("Passage Auto Unlock" in name for name in names)

    async def test_no_switches_for_a_shared_key(
        self, hass, ekey_component_setup, monkeypatch
    ):
        monkeypatch.setattr(
            LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
        )
        await ekey_component_setup(BASIC_EKEY_RECORD_MEMBER)
        assert _switch_names(hass) == []
