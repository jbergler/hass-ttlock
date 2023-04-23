"""Global fixtures for ttlock integration."""

from time import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.const import DOMAIN
from custom_components.ttlock.models import Lock, LockState, PassageModeConfig
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

pytest_plugins = "pytest_homeassistant_custom_component"


# This fixture enables loading custom integrations in all tests.
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


# persistent_notification doesn't exist during tests, patch it so we don't get stuck
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with patch("homeassistant.components.persistent_notification.async_create"), patch(
        "homeassistant.components.persistent_notification.async_dismiss"
    ):
        yield


@pytest.fixture
def config_entry():
    """Mock a config entry."""
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": "mocked",
            "token": {
                "refresh_token": "mock-refresh-token",
                "access_token": "mock-access-token",
                "type": "Bearer",
                "expires_in": 60,
                "expires_at": time() + 1000,
                "scope": "",
            },
        },
    )
    return mock_entry


@pytest.fixture
def component_setup(hass: HomeAssistant, config_entry: MockConfigEntry):
    """Fixture for setting up the integration."""

    async def _setup_func() -> bool:
        assert await async_setup_component(hass, "application_credentials", {})
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential("client-id", "client-secret"),
            config_entry.data["auth_implementation"],
        )
        config_entry.add_to_hass(hass)
        return await hass.config_entries.async_setup(config_entry.entry_id)

    return _setup_func


@pytest.fixture
def sane_default_data():
    """Fixture for mocking sane default data from the API."""

    lock = Lock.parse_obj(
        {
            "date": 1669690212000,
            "lockAlias": "Front Door",
            "lockSound": 2,
            "modelNum": "SN9206_PV53",
            "lockMac": "16:72:4C:CC:01:C4",
            "privacyLock": 2,
            "deletePwd": "",
            "featureValue": "F44354CD5F3",
            "adminPwd": "<REMOVED>",
            "soundVolume": 5,
            "hasGateway": 1,
            "autoLockTime": 60,
            "wirelessKeypadFeatureValue": "0",
            "lockKey": "<REMOVED>",
            "isFrozen": 2,
            "lockName": "S31_c401cc",
            "resetButton": 1,
            "firmwareRevision": "6.0.6.210622",
            "tamperAlert": 1,
            "specialValue": 894227955,
            "displayPasscode": 0,
            "noKeyPwd": "<REMOVED>",
            "passageMode": 1,
            "passageModeAutoUnlock": 2,
            "timezoneRawOffset": 46800000,
            "lockId": 7252408,
            "electricQuantity": 90,
            "lockFlagPos": 0,
            "lockUpdateDate": 1682201024000,
            "keyboardPwdVersion": 4,
            "aesKeyStr": "<REMOVED>",
            "hardwareRevision": "1.6",
            "openDirection": 0,
            "lockVersion": {
                "groupId": 10,
                "protocolVersion": 3,
                "protocolType": 5,
                "orgId": 34,
                "scene": 2,
            },
            "sensitivity": -1,
        }
    )
    state = LockState.parse_obj({"state": 1})
    passage_mode_config = PassageModeConfig.parse_obj(
        {
            "autoUnlock": 2,
            "isAllDay": 2,
            "endDate": 1200,
            "weekDays": [1, 2, 3, 4, 5, 6, 7],
            "passageMode": 1,
            "startDate": 420,
        }
    )

    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks", return_value=[lock.id]
    ), patch(
        "custom_components.ttlock.api.TTLockApi.get_lock", return_value=lock
    ), patch(
        "custom_components.ttlock.api.TTLockApi.get_lock_state", return_value=state
    ), patch(
        "custom_components.ttlock.api.TTLockApi.get_lock_passage_mode_config",
        return_value=passage_mode_config,
    ):
        yield
