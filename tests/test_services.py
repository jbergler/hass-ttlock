"""Test the TTLock services."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import probatio as vol
import pytest

from custom_components.ttlock import coordinator as coord_mod, lock_commands
from custom_components.ttlock.api import RequestFailed
from custom_components.ttlock.ble_protocol import (
    CardRecord,
    FingerprintRecord,
    PasscodeRecord,
    SwitchConfig,
)
from custom_components.ttlock.const import (
    DOMAIN,
    SVC_ADD_CARD,
    SVC_CLEANUP_PASSCODES,
    SVC_CLEAR_CARD_SCHEDULE,
    SVC_CLEAR_CREDENTIALS,
    SVC_CLEAR_FINGERPRINT_SCHEDULE,
    SVC_CONFIG_AUTOLOCK,
    SVC_CONFIG_PASSAGE_MODE,
    SVC_CREATE_PASSCODE,
    SVC_DELETE_CARD,
    SVC_DELETE_FINGERPRINT,
    SVC_DELETE_PASSCODE,
    SVC_ENROLL_FINGERPRINT,
    SVC_GET_LOCK_CONFIG,
    SVC_LIST_CARDS,
    SVC_LIST_FINGERPRINTS,
    SVC_LIST_PASSCODES,
    SVC_LIST_RECORDS,
    SVC_MODIFY_CARD_VALIDITY,
    SVC_MODIFY_FINGERPRINT_VALIDITY,
    SVC_MODIFY_PASSCODE,
    SVC_RENAME_CARD,
    SVC_RENAME_FINGERPRINT,
    SVC_SET_CARD_SCHEDULE,
    SVC_SET_FINGERPRINT_SCHEDULE,
    SVC_SYNC_CLOCK,
    SVC_UPDATE_STATE,
    TT_LOCKS,
    TT_STORE,
    TT_WIFI_PROXY,
)
from custom_components.ttlock.coordinator import (
    LockUpdateCoordinator,
    NoLocalAdminTransport,
)
from custom_components.ttlock.models import (
    AddPasscodeConfig,
    Card,
    Fingerprint,
    LockRecord,
    LockSummary,
    Passcode,
    PasscodeType,
    RecordType,
)
from custom_components.ttlock.number import AutoLockDelay
from custom_components.ttlock.wifi_proxy import MODE_TAKEOVER, WifiCredentials
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.script import Script
from homeassistant.util import dt as dt_util

from .const import BASIC_EKEY_RECORD_MEMBER
from .test_lock_commands import LogLock
from .test_transport import _FakeAdminSession


def answer_locally(coordinator, monkeypatch, replies) -> _FakeAdminSession:
    """Make the WiFi proxy the lock's local path, answering from `replies`.

    The real coordinator runner and command layer run on top, so a refusal in
    `replies` arrives exactly as the user would see it.
    """
    session = _FakeAdminSession(replies)

    async def run(lock_id, op, on_sent):
        on_sent()
        return True, await op(session)

    proxy = MagicMock()
    proxy.session_mode.return_value = MODE_TAKEOVER
    proxy.last_seen.return_value = None
    proxy.async_admin_op = AsyncMock(side_effect=run)
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy
    return session


def _log_locally(coordinator, monkeypatch, lock: LogLock) -> LogLock:
    """Make the WiFi proxy the lock's local path to `lock`'s operation log."""
    session = answer_locally(coordinator, monkeypatch, {})
    monkeypatch.setattr(session, "command", lock.command)
    return lock


@pytest.fixture
async def single_lock_entity_id(mock_api_responses, component_setup) -> str:
    mock_api_responses("default")
    coordinator = await component_setup()
    # Find the lock entity
    lock_entity = next(
        (
            entity
            for entity in coordinator.entities
            if entity.entity_id.startswith("lock.")
        ),
        coordinator.entities[0],
    )
    return lock_entity.entity_id


class Test_configure_autolock:
    @pytest.mark.parametrize(
        ("params", "seconds_expected"),
        [
            ({"enabled": False}, 0),
            ({"enabled": False, "seconds": 30}, 0),
            ({"enabled": True}, 10),
            ({"enabled": True, "seconds": 30}, 30),
        ],
    )
    async def test_configure(
        self,
        hass: HomeAssistant,
        component_setup,
        mock_api_responses,
        params,
        seconds_expected,
    ):
        """Test creating a passcode."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.set_auto_lock", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_CONFIG_AUTOLOCK,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **params,
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [call(coordinator.lock_id, seconds_expected)]


async def test_member_lock_rejects_admin_service_before_api_call(
    hass: HomeAssistant, ekey_component_setup, monkeypatch
):
    """Admin-only services explain the permission issue without calling TTLock."""
    coordinator = await ekey_component_setup(BASIC_EKEY_RECORD_MEMBER)
    request = AsyncMock(return_value=[])
    monkeypatch.setattr(coordinator.api, "list_passcodes", request)
    entity_id = next(
        entity.entity_id
        for entity in coordinator.entities
        if entity.entity_id.startswith("lock.")
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN,
            SVC_LIST_PASSCODES,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
            return_response=True,
        )

    assert exc_info.value.translation_key == "shared_lock_admin_only"
    assert exc_info.value.translation_placeholders == {"entities": entity_id}
    request.assert_not_awaited()


class Test_list_passcodes:
    async def test_list_passcodes(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_passcodes service."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        start_time = dt_util.now() - timedelta(days=1)
        end_time = dt_util.now() + timedelta(weeks=2)
        passcode = Passcode(
            keyboardPwdId=123,
            keyboardPwdType=PasscodeType.temporary,
            keyboardPwdName="Test Code",
            keyboardPwd="123456",
            startDate=int(start_time.timestamp() * 1000),
            endDate=int(end_time.timestamp() * 1000),
        )

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_passcodes",
            return_value=[passcode],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_PASSCODES,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        assert passcode.start_date is not None
        assert passcode.end_date is not None
        assert response == {
            "passcodes": {
                entity_id: [
                    {
                        "name": "Test Code",
                        "id": 123,
                        "passcode": "123456",
                        "type": "period",
                        "start_date": passcode.start_date.isoformat(),
                        "end_date": passcode.end_date.isoformat(),
                        "expired": False,
                    }
                ]
            }
        }

    async def test_list_passcodes_no_results(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_passcodes service with no passcodes."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_passcodes",
            return_value=[],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_PASSCODES,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        assert response == {"passcodes": {entity_id: []}}


class Test_list_records:
    async def test_list_records(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_records service."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        record = LockRecord(
            recordId=123,
            lockId=15450395,
            recordType=RecordType.PASSWORD_UNLOCK,
            success=False,
            username="test",
            keyboardPwd="123456",
            lockDate=int(dt_util.now().timestamp() * 1000),
            serverDate=int(dt_util.now().timestamp() * 1000),
        )

        with patch(
            "custom_components.ttlock.api.TTLockApi.get_lock_records",
            return_value=[record],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_RECORDS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        assert response == {
            "records": {
                entity_id: [
                    {
                        "id": record.id,
                        "lock_id": record.lock_id,
                        "record_type": record.record_type.name
                        if record.record_type is not None
                        else None,
                        "success": record.success,
                        "username": record.username,
                        "keyboard_pwd": record.keyboard_pwd,
                        "lock_date": record.lock_date.isoformat()
                        if record.lock_date
                        else None,
                        "server_date": record.server_date.isoformat()
                        if record.server_date
                        else None,
                    }
                ]
            }
        }

    async def test_list_records_no_results(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_records service with no records."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.get_lock_records",
            return_value=[],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_RECORDS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        assert response == {"records": {entity_id: []}}

    async def test_list_records_with_dates(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_records service with date filters."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        start_time = dt_util.now() - timedelta(days=1)
        end_time = dt_util.now()

        with patch(
            "custom_components.ttlock.api.TTLockApi.get_lock_records",
            return_value=[],
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_LIST_RECORDS,
                {
                    ATTR_ENTITY_ID: entity_id,
                    "start_date": start_time,
                    "end_date": end_time,
                },
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()

            # Verify that timestamps were correctly converted
            kwargs = mock.call_args[1]
            assert kwargs["start_date"] == int(start_time.timestamp() * 1000)
            assert kwargs["end_date"] == int(end_time.timestamp() * 1000)
            assert kwargs["page_no"] == 1
            assert kwargs["page_size"] == 50

    async def test_list_records_with_pagination(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_records service with pagination parameters."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        start_time = dt_util.now() - timedelta(days=1)
        end_time = dt_util.now()

        with patch(
            "custom_components.ttlock.api.TTLockApi.get_lock_records",
            return_value=[],
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_LIST_RECORDS,
                {
                    ATTR_ENTITY_ID: entity_id,
                    "page_no": 2,
                    "page_size": 100,
                    "start_date": start_time,
                    "end_date": end_time,
                },
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()

            kwargs = mock.call_args[1]
            assert kwargs["start_date"] == int(start_time.timestamp() * 1000)
            assert kwargs["end_date"] == int(end_time.timestamp() * 1000)
            assert kwargs["page_no"] == 2
            assert kwargs["page_size"] == 100


class Test_create_passcode:
    async def test_can_create_passcode(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test creating a passcode."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_name": "Test User",
            "passcode": "1234",
            "start_time": dt_util.now() - timedelta(days=1),
            "end_time": dt_util.now() + timedelta(weeks=2),
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(
                    coordinator.lock_id,
                    AddPasscodeConfig(
                        passcode=attrs["passcode"],
                        passcodeName=attrs["passcode_name"],
                        startDate=int(attrs["start_time"].timestamp() * 1000),
                        endDate=int(attrs["end_time"].timestamp() * 1000),
                    ),
                )
            ]

    async def test_create_passcode_failure(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test creating a passcode when the API call fails."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_name": "Test User",
            "passcode": "1234",
            "start_time": dt_util.now() - timedelta(days=1),
            "end_time": dt_util.now() + timedelta(weeks=2),
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_passcode", return_value=False
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.called

    async def test_create_passcode_without_dates(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test creating a passcode without start/end dates (permanent)."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_name": "Test User",
            "passcode": "1234",
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(
                    coordinator.lock_id,
                    AddPasscodeConfig(
                        passcode=attrs["passcode"],
                        passcodeName=attrs["passcode_name"],
                        startDate=None,
                        endDate=None,
                    ),
                )
            ]

    async def test_create_passcode_rejects_start_time_without_end_time(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """Test that create_passcode rejects start_time without end_time."""
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {
                    ATTR_ENTITY_ID: [single_lock_entity_id],
                    "passcode_name": "Test",
                    "passcode": "1234",
                    "start_time": dt_util.now(),
                },
                blocking=True,
            )

    async def test_create_passcode_rejects_end_time_without_start_time(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """Test that create_passcode rejects end_time without start_time."""

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {
                    ATTR_ENTITY_ID: [single_lock_entity_id],
                    "passcode_name": "Test",
                    "passcode": "1234",
                    "end_time": dt_util.now(),
                },
                blocking=True,
            )


class Test_modify_passcode:
    async def test_can_modify_passcode(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test modifying a passcode."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_id": 123,
            "passcode_name": "Updated User",
            "passcode": "5678",
            "start_time": dt_util.now() - timedelta(days=1),
            "end_time": dt_util.now() + timedelta(weeks=2),
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.modify_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(
                    coordinator.lock_id,
                    attrs["passcode_id"],
                    AddPasscodeConfig(
                        passcode=attrs["passcode"],
                        passcodeName=attrs["passcode_name"],
                        startDate=int(attrs["start_time"].timestamp() * 1000),
                        endDate=int(attrs["end_time"].timestamp() * 1000),
                    ),
                )
            ]

    async def test_modify_passcode_failure(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test modifying a passcode when the API call fails."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_id": 123,
            "passcode_name": "Updated User",
            "passcode": "5678",
            "start_time": dt_util.now() - timedelta(days=1),
            "end_time": dt_util.now() + timedelta(weeks=2),
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.modify_passcode", return_value=False
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.called

    async def test_modify_passcode_invalid_entity(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test modifying a passcode with an invalid entity ID."""
        mock_api_responses("default")
        await component_setup()

        attrs = {
            "passcode_id": 123,
            "passcode_name": "Updated User",
            "passcode": "5678",
            "start_time": dt_util.now() - timedelta(days=1),
            "end_time": dt_util.now() + timedelta(weeks=2),
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.modify_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {
                    ATTR_ENTITY_ID: "lock.invalid_entity",
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert not mock.called

    async def test_modify_passcode_without_dates(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test modifying a passcode without start/end dates."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_id": 123,
            "passcode_name": "Updated User",
            "passcode": "5678",
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.modify_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(
                    coordinator.lock_id,
                    attrs["passcode_id"],
                    AddPasscodeConfig(
                        passcode=attrs["passcode"],
                        passcodeName=attrs["passcode_name"],
                        startDate=None,
                        endDate=None,
                    ),
                )
            ]

    async def test_modify_passcode_rejects_start_time_without_end_time(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """Test that modify_passcode rejects start_time without end_time."""

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {
                    ATTR_ENTITY_ID: [single_lock_entity_id],
                    "passcode_id": 123,
                    "passcode_name": "Test",
                    "passcode": "1234",
                    "start_time": dt_util.now(),
                },
                blocking=True,
            )

    async def test_modify_passcode_rejects_end_time_without_start_time(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """Test that modify_passcode rejects end_time without start_time."""

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {
                    ATTR_ENTITY_ID: [single_lock_entity_id],
                    "passcode_id": 123,
                    "passcode_name": "Test",
                    "passcode": "1234",
                    "end_time": dt_util.now(),
                },
                blocking=True,
            )


class Test_delete_passcode:
    async def test_can_delete_passcode(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test deleting a passcode."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_id": 123,
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(
                    coordinator.lock_id,
                    attrs["passcode_id"],
                )
            ]

    async def test_delete_passcode_failure(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test deleting a passcode when the API call fails."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        attrs = {
            "passcode_id": 123,
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_passcode", return_value=False
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert mock.called

    async def test_delete_passcode_invalid_entity(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test deleting a passcode with an invalid entity ID."""
        mock_api_responses("default")
        await component_setup()

        attrs = {
            "passcode_id": 123,
        }
        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_passcode", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_PASSCODE,
                {
                    ATTR_ENTITY_ID: "lock.invalid_entity",
                    **attrs,
                },
            )
            await hass.async_block_till_done()
            assert not mock.called


class Test_cleanup_passcodes:
    @pytest.mark.parametrize("return_response", [True, False])
    async def test_works_when_there_is_nothing_to_do(
        self, hass: HomeAssistant, component_setup, mock_api_responses, return_response
    ) -> None:
        """Test cleanup service with no passcodes."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_passcodes", return_value=[]
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_CLEANUP_PASSCODES,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=return_response,
            )
            await hass.async_block_till_done()
            assert mock.called

        if return_response:
            assert response == {"removed": {}}
        else:
            assert response is None

    async def test_works_when_there_is_an_expired_passcode(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ) -> None:
        """Test cleanup service with an expired passcode."""
        mock_api_responses("default")
        coordinator = await component_setup()
        # Find the lock entity
        lock_entity = next(
            (
                entity
                for entity in coordinator.entities
                if entity.entity_id.startswith("lock.")
            ),
            coordinator.entities[0],
        )
        entity_id = lock_entity.entity_id

        with (
            patch(
                "custom_components.ttlock.api.TTLockApi.list_passcodes",
                return_value=[
                    Passcode(
                        keyboardPwdId=123,
                        keyboardPwdType=PasscodeType.temporary,
                        keyboardPwdName="Test",
                        endDate=0,
                    )
                ],
            ),
            patch(
                "custom_components.ttlock.api.TTLockApi.delete_passcode",
                return_value=True,
            ) as mock,
        ):
            response = await hass.services.async_call(
                DOMAIN,
                SVC_CLEANUP_PASSCODES,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [call(coordinator.lock_id, 123)]

        assert response == {"removed": {entity_id: ["Test"]}}


class Test_update_state:
    async def test_update_state(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test update_state service."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch.object(coordinator, "async_refresh") as mock_refresh:
            await hass.services.async_call(
                DOMAIN,
                SVC_UPDATE_STATE,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
            await hass.async_block_till_done()
            assert mock_refresh.called
            assert coordinator.data.locked is None


class Test_list_cards:
    async def test_list_cards(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_cards service."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        start_time = dt_util.now() - timedelta(days=1)
        end_time = dt_util.now() + timedelta(weeks=2)
        card = Card(
            cardId=124242,
            cardNumber="1723612378",
            cardName="Card for mom",
            cardType=1,
            senderUsername="alexa@google.com",
            startDate=int(start_time.timestamp() * 1000),
            endDate=int(end_time.timestamp() * 1000),
        )

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_cards",
            return_value=[card],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_CARDS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        assert card.start_date is not None
        assert card.end_date is not None
        assert response == {
            "cards": {
                entity_id: [
                    {
                        "id": 124242,
                        "name": "Card for mom",
                        "number": "1723612378",
                        "type": "normal",
                        "sender": "alexa@google.com",
                        "start_date": card.start_date.isoformat(),
                        "end_date": card.end_date.isoformat(),
                        "expired": False,
                    }
                ]
            }
        }

    async def test_list_cards_no_results(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_cards service with no cards."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_cards",
            return_value=[],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_CARDS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        assert response == {"cards": {entity_id: []}}


class Test_rename_card:
    async def test_can_rename_card(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test renaming a card."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.rename_card", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_RENAME_CARD,
                {ATTR_ENTITY_ID: entity_id, "card_id": 124242, "name": "New name"},
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(coordinator.lock_id, 124242, "New name")
            ]


class Test_add_card:
    async def test_can_add_card(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test adding a card, with the new card id in the response."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        start_time = dt_util.now() - timedelta(days=1)
        end_time = dt_util.now() + timedelta(weeks=2)
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_card", return_value=124242
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: entity_id,
                    "card_number": "1723612378",
                    "name": "Card for mom",
                    "start_time": start_time,
                    "end_time": end_time,
                },
                blocking=True,
                return_response=True,
            )

        assert mock.call_args_list == [
            call(
                coordinator.lock_id,
                "1723612378",
                "Card for mom",
                int(start_time.timestamp() * 1000),
                int(end_time.timestamp() * 1000),
                False,
            )
        ]
        assert response == {"cards": {entity_id: {"card_id": 124242, "error": None}}}

    async def test_add_card_without_dates_is_permanent(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """Without start/end the card is sent with 0/0 (permanent)."""
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_card", return_value=124242
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": "1723612378",
                    "name": "Card for mom",
                },
                blocking=True,
            )

        assert mock.call_args.args[1:] == ("1723612378", "Card for mom", 0, 0, False)

    async def test_add_card_accepts_number_as_int(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """A template rendering only digits arrives as an int and is accepted."""
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_card", return_value=124242
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": 1723612378,
                    "name": "Card for mom",
                },
                blocking=True,
            )

        assert mock.call_args.args[1] == "1723612378"

    async def test_add_card_passes_reversed_number(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """reversed_number is passed through to the API."""
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_card", return_value=124242
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": "3661413478",
                    "name": "Card for mom",
                    "reversed_number": True,
                },
                blocking=True,
            )

        assert mock.call_args.args[5] is True

    @pytest.mark.parametrize("card_number", ["", "12ab34", "-1", "1" * 21])
    async def test_add_card_rejects_invalid_number(
        self, single_lock_entity_id, hass: HomeAssistant, card_number
    ):
        """Card numbers must be 1-20 digits."""
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": card_number,
                    "name": "Card for mom",
                },
                blocking=True,
            )

    @pytest.mark.parametrize("given", ["start_time", "end_time"])
    async def test_add_card_rejects_only_one_of_start_and_end(
        self, single_lock_entity_id, hass: HomeAssistant, given
    ):
        """start_time and end_time must be provided together."""
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": "1723612378",
                    "name": "Card for mom",
                    given: dt_util.now(),
                },
                blocking=True,
            )

    async def test_add_card_failure_with_response(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """When the caller asks for the response, a failure is reported in it."""
        with patch(
            "custom_components.ttlock.api.TTLockApi.add_card",
            side_effect=RequestFailed("API returned: errcode -3003"),
        ):
            response = await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": "1723612378",
                    "name": "Card for mom",
                },
                blocking=True,
                return_response=True,
            )

        assert response == {
            "cards": {
                single_lock_entity_id: {
                    "card_id": None,
                    "error": "API returned: errcode -3003",
                }
            }
        }

    async def test_add_card_failure_without_response_raises(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """Without a response to report it in, a failure raises."""
        with (
            patch(
                "custom_components.ttlock.api.TTLockApi.add_card",
                side_effect=RequestFailed("API returned: errcode -3003"),
            ),
            pytest.raises(HomeAssistantError, match=single_lock_entity_id),
        ):
            await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: single_lock_entity_id,
                    "card_number": "1723612378",
                    "name": "Card for mom",
                },
                blocking=True,
            )

    async def test_add_card_one_failing_lock_does_not_hide_the_others(
        self, hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
    ):
        """Each lock is attempted and reported on its own."""
        mock_api_responses("default")

        async def mock_get_locks(*args, **kwargs):
            return [
                LockSummary(
                    lockId=7252408,
                    lockAlias="Front Door",
                    lockMac="00:00:00:00:00:01",
                    hasGateway=1,
                    userType="110301",
                ),
                LockSummary(
                    lockId=2,
                    lockAlias="Back Door",
                    lockMac="00:00:00:00:00:02",
                    hasGateway=1,
                    userType="110301",
                ),
            ]

        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
        )
        await component_setup()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        entity_ids = {
            coordinator.lock_id: coordinator.entities[0].entity_id
            for coordinator in hass.data[DOMAIN][entry.entry_id][TT_LOCKS]
        }

        async def add_card(lock_id, *args):
            if lock_id == 2:
                raise RequestFailed("API returned: errcode -3003")
            return 124242

        with patch(
            "custom_components.ttlock.api.TTLockApi.add_card", side_effect=add_card
        ):
            response = await hass.services.async_call(
                DOMAIN,
                SVC_ADD_CARD,
                {
                    ATTR_ENTITY_ID: [entity_ids[2], entity_ids[7252408]],
                    "card_number": "1723612378",
                    "name": "Card for mom",
                },
                blocking=True,
                return_response=True,
            )

        assert response == {
            "cards": {
                entity_ids[2]: {
                    "card_id": None,
                    "error": "API returned: errcode -3003",
                },
                entity_ids[7252408]: {"card_id": 124242, "error": None},
            }
        }


class Test_delete_card:
    async def test_can_delete_card(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test deleting a card."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_card", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_CARD,
                {ATTR_ENTITY_ID: entity_id, "card_id": 124242},
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [call(coordinator.lock_id, 124242)]

    async def test_delete_card_invalid_entity(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test deleting a card with an invalid entity ID."""
        mock_api_responses("default")
        await component_setup()

        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_card", return_value=True
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_CARD,
                {ATTR_ENTITY_ID: "lock.invalid_entity", "card_id": 124242},
            )
            await hass.async_block_till_done()
            assert not mock.called


class Test_list_fingerprints:
    async def test_list_fingerprints(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_fingerprints service."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        fingerprint = Fingerprint(
            fingerprintId=224242,
            fingerprintNumber="44668054142981",
            fingerprintName="Thumb",
            fingerprintType=1,
            senderUsername="alexa@google.com",
            startDate=0,
            endDate=0,
        )

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_fingerprints",
            return_value=[fingerprint],
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                SVC_LIST_FINGERPRINTS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.called

        # startDate/endDate == 0 means a permanent credential, so no dates and never expired.
        assert response == {
            "fingerprints": {
                entity_id: [
                    {
                        "id": 224242,
                        "name": "Thumb",
                        "number": "44668054142981",
                        "type": "normal",
                        "sender": "alexa@google.com",
                        "start_date": None,
                        "end_date": None,
                        "expired": False,
                    }
                ]
            }
        }


class Test_rename_fingerprint:
    async def test_can_rename_fingerprint(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test renaming a fingerprint."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.rename_fingerprint",
            return_value=True,
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_RENAME_FINGERPRINT,
                {
                    ATTR_ENTITY_ID: entity_id,
                    "fingerprint_id": 224242,
                    "name": "Left thumb",
                },
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [
                call(coordinator.lock_id, 224242, "Left thumb")
            ]


class Test_delete_fingerprint:
    async def test_can_delete_fingerprint(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test deleting a fingerprint."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_fingerprint",
            return_value=True,
        ) as mock:
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_FINGERPRINT,
                {ATTR_ENTITY_ID: entity_id, "fingerprint_id": 224242},
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [call(coordinator.lock_id, 224242)]


class Test_cloud_errors:
    @pytest.mark.parametrize(
        ("service", "method", "data"),
        [
            (SVC_LIST_CARDS, "list_cards", {}),
            (SVC_RENAME_CARD, "rename_card", {"card_id": 124242, "name": "New"}),
            (SVC_DELETE_CARD, "delete_card", {"card_id": 124242}),
        ],
    )
    async def test_cloud_error_is_a_home_assistant_error(
        self, single_lock_entity_id, hass: HomeAssistant, service, method, data
    ):
        """A TTLock errcode surfaces from the action as a HomeAssistantError."""
        with (
            patch(
                f"custom_components.ttlock.api.TTLockApi.{method}",
                side_effect=RequestFailed("API returned: errcode -3003"),
            ),
            pytest.raises(HomeAssistantError),
        ):
            await hass.services.async_call(
                DOMAIN,
                service,
                {ATTR_ENTITY_ID: single_lock_entity_id, **data},
                blocking=True,
                return_response=service == SVC_LIST_CARDS,
            )

    async def test_continue_on_error_skips_a_failed_action(
        self, single_lock_entity_id, hass: HomeAssistant
    ):
        """continue_on_error in a script carries on past a cloud error."""
        step = {
            "action": f"{DOMAIN}.{SVC_DELETE_CARD}",
            "target": {"entity_id": single_lock_entity_id},
            "data": {"card_id": 124242},
            "continue_on_error": True,
        }
        script = Script(hass, cv.SCRIPT_SCHEMA([step, step]), "test", DOMAIN)

        with patch(
            "custom_components.ttlock.api.TTLockApi.delete_card",
            side_effect=RequestFailed("API returned: errcode -3003"),
        ) as mock:
            await script.async_run(context=Context())
            await hass.async_block_till_done()

        assert mock.call_count == 2


async def _lock(component_setup, mock_api_responses):
    """An admin lock with no local transport configured, and its lock entity."""
    mock_api_responses("default")
    coordinator = await component_setup()
    entity_id = next(
        entity.entity_id
        for entity in coordinator.entities
        if entity.entity_id.startswith("lock.")
    )
    return coordinator, entity_id


def _cloud_off(monkeypatch) -> None:
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: False)
    )


class TestCreatePasscodeLocally:
    async def test_goes_to_the_lock_with_local_times(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.add_passcode_local = AsyncMock()
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "add_passcode", cloud)

        await hass.services.async_call(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "passcode_name": "Cleaner",
                "passcode": "1234",
                # naive: Home Assistant's zone, US/Pacific in tests (UTC-8 in January)
                "start_time": "2026-01-02 03:04:00",
                "end_time": "2026-01-03 03:04:00",
            },
            blocking=True,
        )

        coordinator.add_passcode_local.assert_awaited_once_with(
            "1234",
            name="Cleaner",
            start=datetime(2026, 1, 2, 11, 4, tzinfo=UTC),
            end=datetime(2026, 1, 3, 11, 4, tzinfo=UTC),
        )
        cloud.assert_not_awaited()

    async def test_without_a_local_transport_the_cloud_gets_millisecond_epochs(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coordinator.api, "add_passcode", cloud)

        await hass.services.async_call(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "passcode_name": "n",
                "passcode": "1234",
                "start_time": datetime(2026, 1, 1, tzinfo=UTC),
                "end_time": datetime(2026, 1, 2, tzinfo=UTC),
            },
            blocking=True,
        )

        assert cloud.await_args is not None
        config = cloud.await_args.args[1]
        assert config.start_minute == 1767225600000
        assert config.end_minute == 1767312000000

    async def test_a_lock_refusal_never_reaches_the_cloud(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        answer_locally(coordinator, monkeypatch, {0x03: bytes([0x03, 0x00])})
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "add_passcode", cloud)

        with pytest.raises(HomeAssistantError) as excinfo:
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {ATTR_ENTITY_ID: entity_id, "passcode_name": "n", "passcode": "1234"},
                blocking=True,
            )

        assert excinfo.value.translation_key == "local_command_rejected"
        cloud.assert_not_awaited()

    async def test_a_lost_answer_never_reaches_the_cloud(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        answer_locally(coordinator, monkeypatch, {})  # sent, never answered
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "add_passcode", cloud)

        with pytest.raises(HomeAssistantError) as excinfo:
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {ATTR_ENTITY_ID: entity_id, "passcode_name": "n", "passcode": "1234"},
                blocking=True,
            )

        assert excinfo.value.translation_key == "local_outcome_unknown"
        cloud.assert_not_awaited()

    async def test_count_code(self, hass, component_setup, mock_api_responses):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.add_onetime_passcode_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "passcode_name": "Guest",
                "passcode": "1234",
                "code_type": "count",
                "start_time": "2026-07-01 10:00:00",
                "end_time": "2026-07-01 12:00:00",
            },
            blocking=True,
        )

        # US/Pacific is UTC-7 in July
        coordinator.add_onetime_passcode_local.assert_awaited_once_with(
            "1234",
            name="Guest",
            start=datetime(2026, 7, 1, 17, 0, tzinfo=UTC),
            end=datetime(2026, 7, 1, 19, 0, tzinfo=UTC),
        )

    async def test_cyclic_code(self, hass, component_setup, mock_api_responses):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.add_cyclic_passcode_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "passcode_name": "Cleaner",
                "passcode": "1234",
                "code_type": "cyclic",
                "start_time": "2026-01-02 14:30:00",
                "cycle_type": 48,
            },
            blocking=True,
        )

        coordinator.add_cyclic_passcode_local.assert_awaited_once_with(
            "1234",
            name="Cleaner",
            start=datetime(2026, 1, 2, 22, 30, tzinfo=UTC),
            cycle_type=48,
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"passcode": "12"},
            {"passcode": "12ab"},
            {"code_type": "count"},
            {"code_type": "permanent", "start_time": "2026-01-02 03:04:00"},
            {"code_type": "cyclic", "cycle_type": 1},
            {
                "code_type": "cyclic",
                "cycle_type": 1,
                "start_time": "2026-01-02 03:04:00",
                "end_time": "2026-01-03 03:04:00",
            },
            {"code_type": "cyclic", "start_time": "2026-01-02 03:04:00"},
            {"cycle_type": 1},
        ],
    )
    async def test_schema_rejects(
        self, hass, component_setup, mock_api_responses, fields
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.add_passcode_local = AsyncMock()

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_CREATE_PASSCODE,
                {
                    ATTR_ENTITY_ID: entity_id,
                    "passcode_name": "n",
                    "passcode": "1234",
                    **fields,
                },
                blocking=True,
            )
        coordinator.add_passcode_local.assert_not_awaited()


class TestLockTimezone:
    """A date reaches the lock in the lock's own zone, whatever zone it came in."""

    async def _added_window(
        self, hass, component_setup, mock_api_responses, monkeypatch, start, end
    ) -> bytes:
        await hass.config.async_set_time_zone("Europe/Berlin")
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        session = answer_locally(coordinator, monkeypatch, {0x03: bytes([0x03, 0x01])})
        await hass.services.async_call(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "passcode_name": "n",
                "passcode": "1234",
                "start_time": start,
                "end_time": end,
            },
            blocking=True,
        )
        return session.calls[0][1]

    async def test_aware_input_is_written_in_the_lock_zone(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        monkeypatch.setattr(
            LockUpdateCoordinator,
            "_lock_tz",
            property(lambda self: timezone(timedelta(hours=9))),
        )
        # 10:00 in New York (UTC-4 in July) is 14:00 UTC, 23:00 at UTC+9.
        payload = await self._added_window(
            hass,
            component_setup,
            mock_api_responses,
            monkeypatch,
            "2026-07-01T10:00:00-04:00",
            "2026-07-02T10:00:00-04:00",
        )

        assert bytes([26, 7, 1, 23, 0, 26, 7, 2, 23, 0]) in payload

    async def test_naive_input_is_home_assistant_local(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        monkeypatch.setattr(
            LockUpdateCoordinator,
            "_lock_tz",
            property(lambda self: timezone(timedelta(hours=9))),
        )
        # 10:00 in Berlin (UTC+2 in July) is 08:00 UTC, 17:00 at UTC+9.
        payload = await self._added_window(
            hass,
            component_setup,
            mock_api_responses,
            monkeypatch,
            "2026-07-01 10:00:00",
            "2026-07-02 10:00:00",
        )

        assert bytes([26, 7, 1, 17, 0, 26, 7, 2, 17, 0]) in payload

    async def test_unknown_lock_zone_uses_home_assistant_local(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        monkeypatch.setattr(
            LockUpdateCoordinator, "_lock_tz", property(lambda self: None)
        )
        # 10:00 in New York is 14:00 UTC, 16:00 in Berlin.
        payload = await self._added_window(
            hass,
            component_setup,
            mock_api_responses,
            monkeypatch,
            "2026-07-01T10:00:00-04:00",
            "2026-07-02T10:00:00-04:00",
        )

        assert bytes([26, 7, 1, 16, 0, 26, 7, 2, 16, 0]) in payload


class TestModifyAndDeletePasscodeByCode:
    async def test_modify_by_code_goes_to_the_lock(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.modify_passcode_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_MODIFY_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "code": "1234",
                "passcode": "5678",
                "code_type": "period",
                "start_time": "2026-01-02 03:04:00",
                "end_time": "2026-01-03 03:04:00",
            },
            blocking=True,
        )

        coordinator.modify_passcode_local.assert_awaited_once_with(
            "1234",
            "5678",
            start=datetime(2026, 1, 2, 11, 4, tzinfo=UTC),
            end=datetime(2026, 1, 3, 11, 4, tzinfo=UTC),
            pwd_type=3,
        )

    async def test_modify_window_only(self, hass, component_setup, mock_api_responses):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.modify_passcode_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_MODIFY_PASSCODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "code": "1234",
                "start_time": "2026-01-02 03:04:00",
                "end_time": "2026-01-03 03:04:00",
            },
            blocking=True,
        )

        coordinator.modify_passcode_local.assert_awaited_once_with(
            "1234",
            "",
            start=datetime(2026, 1, 2, 11, 4, tzinfo=UTC),
            end=datetime(2026, 1, 3, 11, 4, tzinfo=UTC),
            pwd_type=1,
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"code": "1234"},  # changes nothing
            {"code": "12", "passcode": "5678"},
            {"code": "1234", "passcode": "5678", "start_time": "2026-01-02 03:04:00"},
            {"passcode_id": 1, "code": "1234", "passcode": "5678"},
            {"passcode_id": 1, "passcode": "5678"},  # no passcode_name
            {"passcode_name": "n", "passcode": "5678"},  # no target
        ],
    )
    async def test_modify_schema_rejects(
        self, hass, component_setup, mock_api_responses, monkeypatch, fields
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.modify_passcode_local = AsyncMock()
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "modify_passcode", cloud)

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_MODIFY_PASSCODE,
                {ATTR_ENTITY_ID: entity_id, **fields},
                blocking=True,
            )
        coordinator.modify_passcode_local.assert_not_awaited()
        cloud.assert_not_awaited()

    @pytest.mark.parametrize(
        ("fields", "pwd_type"),
        [({}, 1), ({"code_type": "period"}, 3), ({"code_type": "cyclic"}, 4)],
    )
    async def test_delete_by_code_goes_to_the_lock(
        self, hass, component_setup, mock_api_responses, fields, pwd_type
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.delete_passcode_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_DELETE_PASSCODE,
            {ATTR_ENTITY_ID: entity_id, "code": "1234", **fields},
            blocking=True,
        )

        coordinator.delete_passcode_local.assert_awaited_once_with(
            "1234", pwd_type=pwd_type
        )

    @pytest.mark.parametrize(
        "fields", [{}, {"code": "12"}, {"passcode_id": 1, "code": "1234"}]
    )
    async def test_delete_schema_rejects(
        self, hass, component_setup, mock_api_responses, fields
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.delete_passcode_local = AsyncMock()

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_PASSCODE,
                {ATTR_ENTITY_ID: entity_id, **fields},
                blocking=True,
            )
        coordinator.delete_passcode_local.assert_not_awaited()


class TestCloudOnlyWithTheCloudOff:
    @pytest.mark.parametrize(
        ("service", "fields", "api_method"),
        [
            (
                SVC_MODIFY_PASSCODE,
                {"passcode_id": 1, "passcode_name": "n", "passcode": "5678"},
                "modify_passcode",
            ),
            (SVC_DELETE_PASSCODE, {"passcode_id": 1}, "delete_passcode"),
            (SVC_CLEANUP_PASSCODES, {}, "list_passcodes"),
            (SVC_RENAME_CARD, {"card_id": 1, "name": "n"}, "rename_card"),
            (SVC_DELETE_CARD, {"card_id": 1}, "delete_card"),
            (
                SVC_RENAME_FINGERPRINT,
                {"fingerprint_id": 1, "name": "n"},
                "rename_fingerprint",
            ),
            (SVC_DELETE_FINGERPRINT, {"fingerprint_id": 1}, "delete_fingerprint"),
            (SVC_LIST_PASSCODES, {"source": "cloud"}, "list_passcodes"),
            (SVC_LIST_CARDS, {"source": "cloud"}, "list_cards"),
            (SVC_LIST_FINGERPRINTS, {"source": "cloud"}, "list_fingerprints"),
            (SVC_LIST_RECORDS, {"source": "cloud"}, "get_lock_records"),
        ],
    )
    async def test_is_refused_without_calling_the_cloud(
        self,
        hass,
        component_setup,
        mock_api_responses,
        monkeypatch,
        service,
        fields,
        api_method,
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        _cloud_off(monkeypatch)
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, api_method, cloud)

        with pytest.raises(ServiceValidationError) as excinfo:
            await hass.services.async_call(
                DOMAIN,
                service,
                {ATTR_ENTITY_ID: entity_id, **fields},
                blocking=True,
                return_response=service.startswith("list_"),
            )

        assert excinfo.value.translation_key == "cloud_disabled"
        cloud.assert_not_awaited()


class TestListFromTheLock:
    async def _list(self, hass, service, entity_id, **fields):
        return await hass.services.async_call(
            DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, **fields},
            blocking=True,
            return_response=True,
        )

    async def test_passcodes(self, hass, component_setup, mock_api_responses):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.async_run_local_admin_op = AsyncMock(
            return_value=[
                PasscodeRecord(
                    pwd_type=4,
                    passcode="1234",
                    new_passcode="5678",
                    start=datetime(2026, 1, 2, 3, 4),
                    end=None,
                    cycle_type=48,
                )
            ]
        )

        result = await self._list(hass, SVC_LIST_PASSCODES, entity_id, source="lock")

        coordinator.async_run_local_admin_op.assert_awaited_once_with(
            "list passcodes",
            lock_commands.list_passcodes,
            idempotent=True,
            read_only=True,
        )
        assert result["passcodes"][entity_id] == [
            {
                "name": None,
                "id": None,
                "passcode": "1234",
                "type": None,
                "start_date": "2026-01-02T03:04:00",
                "end_date": None,
                "expired": None,
                "new_passcode": "5678",
                "code_type": "cyclic",
                "cycle_type": 48,
            }
        ]

    async def test_cards_by_default_when_the_cloud_is_off(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        _cloud_off(monkeypatch)
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "list_cards", cloud)
        coordinator.async_run_local_admin_op = AsyncMock(
            return_value=[
                CardRecord(
                    card_number="1234567890",
                    start=datetime(2026, 1, 2, 3, 4),
                    end=datetime(2026, 1, 3, 3, 4),
                )
            ]
        )

        result = await self._list(hass, SVC_LIST_CARDS, entity_id)

        coordinator.async_run_local_admin_op.assert_awaited_once_with(
            "list cards", lock_commands.list_cards, idempotent=True, read_only=True
        )
        assert result["cards"][entity_id] == [
            {
                "id": None,
                "name": None,
                "number": "1234567890",
                "type": None,
                "sender": None,
                "start_date": "2026-01-02T03:04:00",
                "end_date": "2026-01-03T03:04:00",
                "expired": None,
            }
        ]
        cloud.assert_not_awaited()

    async def test_fingerprints(self, hass, component_setup, mock_api_responses):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.async_run_local_admin_op = AsyncMock(
            return_value=[
                FingerprintRecord(fingerprint_number="99", start=None, end=None)
            ]
        )

        result = await self._list(hass, SVC_LIST_FINGERPRINTS, entity_id, source="lock")

        coordinator.async_run_local_admin_op.assert_awaited_once_with(
            "list fingerprints",
            lock_commands.list_fingerprints,
            idempotent=True,
            read_only=True,
        )
        assert result["fingerprints"][entity_id] == [
            {
                "id": None,
                "name": None,
                "number": "99",
                "type": None,
                "sender": None,
                "start_date": None,
                "end_date": None,
                "expired": None,
            }
        ]

    async def test_explicit_lock_source_does_not_fall_back(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "list_cards", cloud)

        with pytest.raises(NoLocalAdminTransport):
            await self._list(hass, SVC_LIST_CARDS, entity_id, source="lock")

        cloud.assert_not_awaited()

    async def test_records_are_paged_newest_first(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        lock = _log_locally(coordinator, monkeypatch, LogLock(5))

        result = await self._list(
            hass, SVC_LIST_RECORDS, entity_id, source="lock", page_size=2, page_no=2
        )

        assert result["records"][entity_id] == [
            {
                "id": lock.ids[n],
                "lock_id": coordinator.lock_id,
                "record_type": None,
                "success": None,
                "username": None,
                "keyboard_pwd": None,
                "lock_date": "2026-01-01T00:00:00",
                "server_date": None,
                "lock_record_type": 1,
                "battery": 0x50,
            }
            for n in (2, 1)
        ]
        assert result["complete"] == {entity_id: True}

    async def test_records_resume_from_the_stored_cursor(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        lock = _log_locally(coordinator, monkeypatch, LogLock(300))

        result = await self._list(hass, SVC_LIST_RECORDS, entity_id, source="lock")

        assert result["complete"] == {entity_id: False}
        assert [r["id"] for r in result["records"][entity_id]][:2] == [
            lock.ids[99],
            lock.ids[98],
        ]
        store = hass.data[DOMAIN][TT_STORE]
        stored = (await store.async_get(coordinator.lock_id))["operate_log"]
        assert stored["last_sequence"] == 99
        assert len(stored["records"]) == 100

        coordinator._operate_log = None  # noqa: SLF001 - as after a restart
        lock.calls.clear()
        await self._list(hass, SVC_LIST_RECORDS, entity_id, source="lock")
        assert lock.calls[:2] == [99, 100]

    async def test_records_stop_between_pages_before_the_time_limit(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        monkeypatch.setattr(coord_mod, "LOG_READ_TIME_LIMIT", 0.3)
        monkeypatch.setattr(coord_mod, "LOG_READ_MARGIN", 0.1)
        lock = _log_locally(coordinator, monkeypatch, LogLock(300, delay=0.05))
        loop = asyncio.get_running_loop()
        started = loop.time()

        result = await self._list(hass, SVC_LIST_RECORDS, entity_id, source="lock")

        assert loop.time() - started < 0.3
        assert not lock.cancelled
        assert result["complete"] == {entity_id: False}
        read = len(result["records"][entity_id])
        assert 0 < read == len(lock.calls)
        stored = await hass.data[DOMAIN][TT_STORE].async_get(coordinator.lock_id)
        assert len(stored["operate_log"]["records"]) == read

    async def test_a_hung_command_is_cut_at_the_time_limit(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        monkeypatch.setattr(coord_mod, "LOG_READ_TIME_LIMIT", 0.2)
        monkeypatch.setattr(coord_mod, "LOG_READ_MARGIN", 0.1)
        lock = _log_locally(coordinator, monkeypatch, LogLock(300, delay=5))
        loop = asyncio.get_running_loop()
        started = loop.time()

        result = await self._list(hass, SVC_LIST_RECORDS, entity_id, source="lock")

        assert loop.time() - started < 1
        assert lock.cancelled
        assert result == {"records": {entity_id: []}, "complete": {entity_id: False}}

    async def test_a_write_goes_before_the_next_page_of_a_read(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        """The read stops between pages, keeps its place and goes on after."""
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        lock = _log_locally(coordinator, monkeypatch, LogLock(20, delay=0.05))
        read = hass.async_create_task(
            self._list(hass, SVC_LIST_RECORDS, entity_id, source="lock")
        )
        while len(lock.calls) < 3:
            await asyncio.sleep(0.01)
        before = len(lock.calls)
        loop = asyncio.get_running_loop()
        started = loop.time()
        pages: list[int] = []

        async def write(session):
            pages.append(len(lock.calls))
            return "written"

        assert (
            await coordinator.async_run_local_admin_op("write", write, idempotent=False)
            == "written"
        )
        assert pages == [before]  # the page in flight, and no other
        assert loop.time() - started < 1
        result = await read
        assert result["complete"] == {entity_id: True}
        assert [r["id"] for r in result["records"][entity_id]] == lock.ids[::-1]
        # Each page once, the page the read stopped on again, then the end.
        assert len(lock.calls) == 20 + 1 + 1
        assert lock.calls[pages[0]] == lock.calls[pages[0] - 1]
        assert not lock.cancelled

    async def test_records_refuse_a_date_filter(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.async_run_local_admin_op = AsyncMock(return_value=[])

        with pytest.raises(ServiceValidationError) as excinfo:
            await self._list(
                hass,
                SVC_LIST_RECORDS,
                entity_id,
                source="lock",
                start_date="2026-01-02 03:04:00",
            )

        assert excinfo.value.translation_key == "lock_log_dates"
        coordinator.async_run_local_admin_op.assert_not_awaited()


class TestPassageModeLocally:
    async def test_enable_goes_to_the_lock(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.set_passage_mode_local = AsyncMock()
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "set_passage_mode", cloud)

        await hass.services.async_call(
            DOMAIN,
            SVC_CONFIG_PASSAGE_MODE,
            {
                ATTR_ENTITY_ID: entity_id,
                "enabled": True,
                "days": ["mon", "wed"],
                "start_time": "08:00",
                "end_time": "17:30",
            },
            blocking=True,
        )

        coordinator.set_passage_mode_local.assert_awaited_once_with(
            week_days=[1, 3],
            start_minute=480,
            end_minute=1050,
            all_day=False,
            auto_unlock=None,
        )
        cloud.assert_not_awaited()

    async def test_disable_clears_the_lock(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.clear_passage_mode_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_CONFIG_PASSAGE_MODE,
            {ATTR_ENTITY_ID: entity_id, "enabled": False},
            blocking=True,
        )

        coordinator.clear_passage_mode_local.assert_awaited_once_with()

    async def test_without_a_local_transport_the_cloud_takes_it(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coordinator.api, "set_passage_mode", cloud)

        await hass.services.async_call(
            DOMAIN,
            SVC_CONFIG_PASSAGE_MODE,
            {ATTR_ENTITY_ID: entity_id, "enabled": True, "all_day": True},
            blocking=True,
        )

        assert cloud.await_args is not None
        config = cloud.await_args.args[1]
        assert config.model_dump(by_alias=True, exclude_none=True, mode="json") == {
            "passageMode": 1,
            "autoUnlock": 2,
            "isAllDay": 1,
            "startDate": 0,
            "endDate": 0,
            "weekDays": [1, 2, 3, 4, 5, 6, 7],
        }
        assert coordinator.data.passage_mode_config == config

    async def test_with_the_cloud_off_and_no_local_transport_it_fails(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        _cloud_off(monkeypatch)
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "set_passage_mode", cloud)

        with pytest.raises(NoLocalAdminTransport):
            await hass.services.async_call(
                DOMAIN,
                SVC_CONFIG_PASSAGE_MODE,
                {ATTR_ENTITY_ID: entity_id, "enabled": True, "all_day": True},
                blocking=True,
            )

        cloud.assert_not_awaited()


@pytest.mark.parametrize(
    ("service", "method", "fields"),
    [
        (
            SVC_SET_CARD_SCHEDULE,
            "set_card_schedule_local",
            {"card_number": "42", "start_time": "07:15", "end_time": "19:45"},
        ),
        (
            SVC_SET_FINGERPRINT_SCHEDULE,
            "set_fingerprint_schedule_local",
            {"fingerprint_number": "42", "start_time": "07:15", "end_time": "19:45"},
        ),
        (
            SVC_CONFIG_PASSAGE_MODE,
            "set_passage_mode_local",
            {"enabled": True, "start_time": "08:00", "end_time": "17:30"},
        ),
    ],
)
async def test_an_empty_days_list_is_rejected(
    hass, component_setup, mock_api_responses, service, method, fields
):
    """No days would clear the schedule and add none, leaving it unrestricted."""
    coordinator, entity_id = await _lock(component_setup, mock_api_responses)
    setattr(coordinator, method, AsyncMock())

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, "days": [], **fields},
            blocking=True,
        )
    getattr(coordinator, method).assert_not_awaited()


class TestAutolockLocally:
    async def test_goes_through_the_local_first_setter(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.set_auto_lock_seconds = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_CONFIG_AUTOLOCK,
            {ATTR_ENTITY_ID: entity_id, "enabled": True, "seconds": 900},
            blocking=True,
        )

        coordinator.set_auto_lock_seconds.assert_awaited_once_with(900)

    async def test_the_schema_maximum_is_the_number_entity_maximum(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        number = next(e for e in coordinator.entities if isinstance(e, AutoLockDelay))

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_CONFIG_AUTOLOCK,
                {
                    ATTR_ENTITY_ID: entity_id,
                    "enabled": True,
                    "seconds": int(number.native_max_value) + 1,
                },
                blocking=True,
            )


class TestCardsAndFingerprintsLocally:
    async def test_add_card_goes_to_the_lock(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.add_card_local = AsyncMock(return_value=None)
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "add_card", cloud)

        result = await hass.services.async_call(
            DOMAIN,
            SVC_ADD_CARD,
            {ATTR_ENTITY_ID: entity_id, "card_number": "1234567890", "name": "Fob"},
            blocking=True,
            return_response=True,
        )

        coordinator.add_card_local.assert_awaited_once_with(
            "1234567890", name="Fob", start=None, end=None
        )
        cloud.assert_not_awaited()
        assert result["cards"][entity_id] == {"card_id": None, "error": None}

    async def test_a_reversed_number_goes_to_the_cloud_only(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.add_card_local = AsyncMock()
        cloud = AsyncMock(return_value=7)
        monkeypatch.setattr(coordinator.api, "add_card", cloud)

        await hass.services.async_call(
            DOMAIN,
            SVC_ADD_CARD,
            {
                ATTR_ENTITY_ID: entity_id,
                "card_number": "1234567890",
                "name": "Fob",
                "reversed_number": True,
            },
            blocking=True,
        )

        coordinator.add_card_local.assert_not_awaited()
        cloud.assert_awaited_once_with(
            coordinator.lock_id, "1234567890", "Fob", 0, 0, True
        )

    async def test_a_card_the_lock_refuses_is_reported_not_retried(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        answer_locally(coordinator, monkeypatch, {0x05: bytes([0x05, 0x00])})
        cloud = AsyncMock()
        monkeypatch.setattr(coordinator.api, "add_card", cloud)

        result = await hass.services.async_call(
            DOMAIN,
            SVC_ADD_CARD,
            {ATTR_ENTITY_ID: entity_id, "card_number": "1234567890", "name": "Fob"},
            blocking=True,
            return_response=True,
        )

        assert result["cards"][entity_id]["card_id"] is None
        assert result["cards"][entity_id]["error"]
        cloud.assert_not_awaited()

    async def test_delete_card_by_number(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.delete_card_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_DELETE_CARD,
            {ATTR_ENTITY_ID: entity_id, "card_number": "1234567890"},
            blocking=True,
        )

        coordinator.delete_card_local.assert_awaited_once_with("1234567890")

    @pytest.mark.parametrize(
        "fields", [{}, {"card_number": "12a"}, {"card_id": 1, "card_number": "1"}]
    )
    async def test_delete_card_schema_rejects(
        self, hass, component_setup, mock_api_responses, fields
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.delete_card_local = AsyncMock()

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_CARD,
                {ATTR_ENTITY_ID: entity_id, **fields},
                blocking=True,
            )
        coordinator.delete_card_local.assert_not_awaited()

    async def test_delete_fingerprint_by_number(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.delete_fingerprint_local = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_DELETE_FINGERPRINT,
            {ATTR_ENTITY_ID: entity_id, "fingerprint_number": "42"},
            blocking=True,
        )

        coordinator.delete_fingerprint_local.assert_awaited_once_with("42")

    @pytest.mark.parametrize("number", ["0", str(1 << 48), "4x"])
    async def test_fingerprint_number_out_of_range_is_refused(
        self, hass, component_setup, mock_api_responses, number
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.delete_fingerprint_local = AsyncMock()

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SVC_DELETE_FINGERPRINT,
                {ATTR_ENTITY_ID: entity_id, "fingerprint_number": number},
                blocking=True,
            )
        coordinator.delete_fingerprint_local.assert_not_awaited()

    @pytest.mark.parametrize(
        ("service", "method", "number_field"),
        [
            (SVC_MODIFY_CARD_VALIDITY, "modify_card_validity_local", "card_number"),
            (
                SVC_MODIFY_FINGERPRINT_VALIDITY,
                "modify_fingerprint_validity_local",
                "fingerprint_number",
            ),
        ],
    )
    async def test_validity(
        self, hass, component_setup, mock_api_responses, service, method, number_field
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        setattr(coordinator, method, AsyncMock())

        await hass.services.async_call(
            DOMAIN,
            service,
            {
                ATTR_ENTITY_ID: entity_id,
                number_field: "42",
                "start_time": "2026-01-02 03:04:00",
                "end_time": "2026-01-03 03:04:00",
            },
            blocking=True,
        )

        getattr(coordinator, method).assert_awaited_once_with(
            "42",
            datetime(2026, 1, 2, 11, 4, tzinfo=UTC),
            datetime(2026, 1, 3, 11, 4, tzinfo=UTC),
        )

    @pytest.mark.parametrize(
        ("service", "method", "number_field"),
        [
            (SVC_SET_CARD_SCHEDULE, "set_card_schedule_local", "card_number"),
            (
                SVC_SET_FINGERPRINT_SCHEDULE,
                "set_fingerprint_schedule_local",
                "fingerprint_number",
            ),
        ],
    )
    async def test_schedule(
        self, hass, component_setup, mock_api_responses, service, method, number_field
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        setattr(coordinator, method, AsyncMock())

        await hass.services.async_call(
            DOMAIN,
            service,
            {
                ATTR_ENTITY_ID: entity_id,
                number_field: "42",
                "days": ["tue", "sun"],
                "start_time": "07:15",
                "end_time": "19:45",
            },
            blocking=True,
        )

        getattr(coordinator, method).assert_awaited_once_with(
            "42", week_days=[2, 7], start_minute=435, end_minute=1185
        )

    @pytest.mark.parametrize(
        ("service", "method", "number_field"),
        [
            (SVC_CLEAR_CARD_SCHEDULE, "clear_card_cyclic_local", "card_number"),
            (
                SVC_CLEAR_FINGERPRINT_SCHEDULE,
                "clear_fingerprint_cyclic_local",
                "fingerprint_number",
            ),
        ],
    )
    async def test_clear_schedule(
        self, hass, component_setup, mock_api_responses, service, method, number_field
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        setattr(coordinator, method, AsyncMock())

        await hass.services.async_call(
            DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, number_field: "42"},
            blocking=True,
        )

        getattr(coordinator, method).assert_awaited_once_with("42")

    async def test_enroll_fingerprint_returns_the_number(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.enroll_fingerprint_local = AsyncMock(return_value=1234567890)

        result = await hass.services.async_call(
            DOMAIN,
            SVC_ENROLL_FINGERPRINT,
            {ATTR_ENTITY_ID: entity_id, "name": "Thumb"},
            blocking=True,
            return_response=True,
        )

        coordinator.enroll_fingerprint_local.assert_awaited_once_with(name="Thumb")
        assert result == {"fingerprints": {entity_id: {"number": "1234567890"}}}

    async def test_enroll_fingerprint_without_the_wifi_proxy_fails(
        self, hass, component_setup, mock_api_responses
    ):
        _coordinator, entity_id = await _lock(component_setup, mock_api_responses)

        with pytest.raises(NoLocalAdminTransport):
            await hass.services.async_call(
                DOMAIN,
                SVC_ENROLL_FINGERPRINT,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )


class TestLockMaintenance:
    async def test_sync_clock_defaults_to_now(
        self, hass, component_setup, mock_api_responses, freezer
    ):
        freezer.move_to("2026-03-04 05:06:07+00:00")
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.sync_clock = AsyncMock()

        await hass.services.async_call(
            DOMAIN, SVC_SYNC_CLOCK, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

        coordinator.sync_clock.assert_awaited_once_with(
            datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        )

    async def test_sync_clock_to_a_given_time(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.sync_clock = AsyncMock()

        await hass.services.async_call(
            DOMAIN,
            SVC_SYNC_CLOCK,
            {ATTR_ENTITY_ID: entity_id, "time": "2026-01-02 03:04:05"},
            blocking=True,
        )

        coordinator.sync_clock.assert_awaited_once_with(
            datetime(2026, 1, 2, 11, 4, 5, tzinfo=UTC)
        )

    @pytest.mark.parametrize(
        ("credential_type", "method"),
        [
            ("passcodes", "clear_passcodes_local"),
            ("cards", "clear_cards_local"),
            ("fingerprints", "clear_fingerprints_local"),
        ],
    )
    async def test_clear_credentials(
        self, hass, component_setup, mock_api_responses, credential_type, method
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        clears = {
            name: AsyncMock()
            for name in (
                "clear_passcodes_local",
                "clear_cards_local",
                "clear_fingerprints_local",
            )
        }
        for name, mock in clears.items():
            setattr(coordinator, name, mock)

        await hass.services.async_call(
            DOMAIN,
            SVC_CLEAR_CREDENTIALS,
            {ATTR_ENTITY_ID: entity_id, "credential_type": credential_type},
            blocking=True,
        )

        for name, mock in clears.items():
            assert mock.await_count == (name == method)

    async def test_get_lock_config(self, hass, component_setup, mock_api_responses):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.async_run_local_admin_op = AsyncMock(
            return_value=SwitchConfig(
                switch_item=0x07,
                switch_value=0x21,
                settings={"tamper_alert": True, "privacy_lock": False},
            )
        )

        result = await hass.services.async_call(
            DOMAIN,
            SVC_GET_LOCK_CONFIG,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
            return_response=True,
        )

        coordinator.async_run_local_admin_op.assert_awaited_once_with(
            "read lock config",
            lock_commands.get_switch_config,
            idempotent=True,
            read_only=True,
        )
        assert result == {
            "config": {
                entity_id: {
                    "switch_item": 0x07,
                    "switch_value": 0x21,
                    "tamper_alert": True,
                    "privacy_lock": False,
                }
            }
        }

    async def test_get_lock_config_unreadable(
        self, hass, component_setup, mock_api_responses
    ):
        coordinator, entity_id = await _lock(component_setup, mock_api_responses)
        coordinator.async_run_local_admin_op = AsyncMock(return_value=None)

        with pytest.raises(HomeAssistantError) as excinfo:
            await hass.services.async_call(
                DOMAIN,
                SVC_GET_LOCK_CONFIG,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )

        assert excinfo.value.translation_key == "lock_config_unreadable"
        coordinator.async_run_local_admin_op.assert_awaited_once_with(
            "read lock config",
            lock_commands.get_switch_config,
            idempotent=True,
            read_only=True,
        )


_MEMBER_PAYLOADS = {
    SVC_CONFIG_PASSAGE_MODE: {"enabled": False},
    SVC_CREATE_PASSCODE: {"passcode_name": "n", "passcode": "1234"},
    SVC_MODIFY_PASSCODE: {"code": "1234", "passcode": "5678"},
    SVC_DELETE_PASSCODE: {"code": "1234"},
    SVC_CLEANUP_PASSCODES: {},
    SVC_LIST_PASSCODES: {},
    SVC_LIST_RECORDS: {},
    SVC_CONFIG_AUTOLOCK: {"enabled": True},
    SVC_LIST_CARDS: {},
    SVC_RENAME_CARD: {"card_id": 1, "name": "n"},
    SVC_ADD_CARD: {"card_number": "123", "name": "n"},
    SVC_DELETE_CARD: {"card_number": "123"},
    SVC_LIST_FINGERPRINTS: {},
    SVC_RENAME_FINGERPRINT: {"fingerprint_id": 1, "name": "n"},
    SVC_DELETE_FINGERPRINT: {"fingerprint_number": "1"},
    SVC_SYNC_CLOCK: {},
    SVC_CLEAR_CREDENTIALS: {"credential_type": "cards"},
    SVC_MODIFY_CARD_VALIDITY: {
        "card_number": "1",
        "start_time": "2026-01-02 03:04:00",
        "end_time": "2026-01-03 03:04:00",
    },
    SVC_SET_CARD_SCHEDULE: {
        "card_number": "1",
        "days": ["mon"],
        "start_time": "08:00",
        "end_time": "09:00",
    },
    SVC_CLEAR_CARD_SCHEDULE: {"card_number": "1"},
    SVC_ENROLL_FINGERPRINT: {},
    SVC_MODIFY_FINGERPRINT_VALIDITY: {
        "fingerprint_number": "1",
        "start_time": "2026-01-02 03:04:00",
        "end_time": "2026-01-03 03:04:00",
    },
    SVC_SET_FINGERPRINT_SCHEDULE: {
        "fingerprint_number": "1",
        "days": ["mon"],
        "start_time": "08:00",
        "end_time": "09:00",
    },
    SVC_CLEAR_FINGERPRINT_SCHEDULE: {"fingerprint_number": "1"},
    SVC_GET_LOCK_CONFIG: {},
}


async def test_every_admin_service_is_covered_by_the_member_check(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("default")
    await component_setup()

    assert set(_MEMBER_PAYLOADS) == set(hass.services.async_services()[DOMAIN]) - {
        SVC_UPDATE_STATE
    }


@pytest.mark.parametrize(("service", "fields"), _MEMBER_PAYLOADS.items())
async def test_a_shared_key_reaches_neither_the_lock_nor_the_cloud(
    hass, ekey_component_setup, monkeypatch, service, fields
):
    coordinator = await ekey_component_setup(BASIC_EKEY_RECORD_MEMBER)
    entity_id = next(
        entity.entity_id
        for entity in coordinator.entities
        if entity.entity_id.startswith("lock.")
    )
    run = AsyncMock()
    enroll = AsyncMock()
    monkeypatch.setattr(coordinator, "async_run_local_admin_op", run)
    monkeypatch.setattr(coordinator, "enroll_fingerprint_local", enroll)
    api = MagicMock()
    monkeypatch.setattr(coordinator, "api", api)

    with pytest.raises(ServiceValidationError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, **fields},
            blocking=True,
            return_response=hass.services.supports_response(DOMAIN, service)
            is SupportsResponse.ONLY,
        )

    assert excinfo.value.translation_key == "shared_lock_admin_only"
    run.assert_not_awaited()
    enroll.assert_not_awaited()
    assert api.mock_calls == []
