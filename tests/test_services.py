"""Test the TTLock services."""

from datetime import timedelta
from unittest.mock import call, patch

import pytest

from custom_components.ttlock.const import (
    DOMAIN,
    SVC_CLEANUP_PASSCODES,
    SVC_CONFIG_AUTOLOCK,
    SVC_CREATE_PASSCODE,
    SVC_LIST_PASSCODES,
    SVC_LIST_RECORDS,
)
from custom_components.ttlock.models import (
    AddPasscodeConfig,
    LockRecord,
    Passcode,
    PasscodeType,
    RecordType,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt


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


class Test_list_passcodes:
    async def test_list_passcodes(
        self, hass: HomeAssistant, component_setup, mock_api_responses
    ):
        """Test list_passcodes service."""
        mock_api_responses("default")
        coordinator = await component_setup()
        entity_id = coordinator.entities[0].entity_id

        start_time = dt.now() - timedelta(days=1)
        end_time = dt.now() + timedelta(weeks=2)
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

        assert response == {
            "passcodes": {
                entity_id: [
                    {
                        "name": "Test Code",
                        "passcode": "123456",
                        "type": "temporary",
                        "start_date": passcode.start_date,
                        "end_date": passcode.end_date,
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
            recordTypeFromLock=7,
            success=False,
            username="test",
            keyboardPwd="123456",
            lockDate=int(dt.now().timestamp() * 1000),
            serverDate=int(dt.now().timestamp() * 1000),
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
                        "record_type": record.record_type.name,
                        "success": record.success,
                        "username": record.username,
                        "keyboard_pwd": record.keyboard_pwd,
                        "lock_date": record.lock_date,
                        "server_date": record.server_date,
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

        start_time = dt.now() - timedelta(days=1)
        end_time = dt.now()

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

        start_time = dt.now() - timedelta(days=1)
        end_time = dt.now()

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
            "passcode": 1234,
            "start_time": dt.now() - timedelta(days=1),
            "end_time": dt.now() + timedelta(weeks=2),
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


class Test_cleanup_passcodes:
    @pytest.mark.parametrize("return_response", (True, False))
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
        entity_id = coordinator.entities[0].entity_id

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
