"""Test the TTLockApi REST client."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientResponseError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ttlock.api import (
    RequestFailed,
    TTLockApi,
    TTLockAuthImplementation,
)
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.models import (
    AddPasscodeConfig,
    Card,
    CardType,
    Fingerprint,
    OnOff,
    PassageModeConfig,
)
from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant

from .const import BASIC_LOCK_DETAILS, PASSAGE_MODE_6_TO_6_7_DAYS

BASE = "https://euapi.ttlock.com/v3/"


@pytest.fixture
def mock_oauth_session():
    """A minimal stand-in for OAuth2Session."""
    session = MagicMock()
    session.valid_token = True
    session.token = {"access_token": "mock-access-token"}
    session.implementation.client_id = "mock-client-id"
    session.async_ensure_token_valid = AsyncMock()
    return session


@pytest.fixture
async def mocker():
    """An AiohttpClientMocker instance, not bound to any event loop."""
    return AiohttpClientMocker()


@pytest.fixture
async def ttlock_api(mock_oauth_session, mocker: AiohttpClientMocker):
    """A TTLockApi instance backed by the aiohttp mocker."""
    session = mocker.create_session(None)
    yield TTLockApi(session, mock_oauth_session)
    await session.close()


class TestAuth:
    async def test_async_get_access_token_reuses_valid_token(
        self, ttlock_api: TTLockApi, mock_oauth_session
    ):
        mock_oauth_session.valid_token = True

        token = await ttlock_api.async_get_access_token()

        assert token == "mock-access-token"
        mock_oauth_session.async_ensure_token_valid.assert_not_called()

    async def test_async_get_access_token_refreshes_invalid_token(
        self, ttlock_api: TTLockApi, mock_oauth_session
    ):
        mock_oauth_session.valid_token = False

        token = await ttlock_api.async_get_access_token()

        assert token == "mock-access-token"
        mock_oauth_session.async_ensure_token_valid.assert_awaited_once()

    async def test_add_auth_includes_client_id_and_token_and_date(
        self, ttlock_api: TTLockApi
    ):
        kwargs = await ttlock_api._add_auth(foo="bar")

        assert kwargs["foo"] == "bar"
        assert kwargs["clientId"] == "mock-client-id"
        assert kwargs["accessToken"] == "mock-access-token"
        assert int(kwargs["date"]) > 0


class TestGetAndPost:
    async def test_get_builds_url_and_returns_json(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lock/detail", json={"errcode": 0, "lockId": 1})

        res = await ttlock_api.get("lock/detail", lockId=1)

        assert res == {"errcode": 0, "lockId": 1}
        assert mocker.mock_calls[0][1].query["lockId"] == "1"
        assert mocker.mock_calls[0][1].query["clientId"] == "mock-client-id"

    async def test_post_sends_form_data(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}lock/lock", json={"errcode": 0})

        res = await ttlock_api.post("lock/lock", lockId=1)

        assert res == {"errcode": 0}
        assert mocker.mock_calls[0][2] == {"lockId": 1}

    async def test_parse_resp_raises_on_http_error(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lock/detail", status=500, text="boom")

        with pytest.raises(ClientResponseError):
            await ttlock_api.get("lock/detail", lockId=1)

    async def test_parse_resp_raises_request_failed_on_errcode(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(
            f"{BASE}lock/detail", json={"errcode": 10003, "errmsg": "lock not found"}
        )

        with pytest.raises(RequestFailed):
            await ttlock_api.get("lock/detail", lockId=1)


class TestPerLockLogging:
    async def test_debug_on_one_lock_logger_does_not_capture_another_locks_records(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker, caplog
    ):
        # pytest_homeassistant_custom_component forces the root logger to
        # DEBUG for every test, so lock 2's logger needs an explicit level
        # above DEBUG here to simulate its normal, un-enabled default -
        # otherwise every logger would already capture everything and this
        # test couldn't tell isolation from coincidence.
        mocker.get(f"{BASE}lock/detail", json={"errcode": 0})
        mocker.get(f"{BASE}lock/queryOpenState", json={"errcode": 0})
        # order matters: caplog's set_level shares one handler whose level is
        # overwritten by each call, so the DEBUG call must come last to keep
        # the handler itself open to DEBUG records - device.2 is still
        # excluded because its *logger* (not the shared handler) stays at
        # WARNING.
        caplog.set_level(logging.WARNING, logger="custom_components.ttlock.device.2")
        caplog.set_level(logging.DEBUG, logger="custom_components.ttlock.device.1")

        await ttlock_api.get("lock/detail", lockId=1)
        await ttlock_api.get("lock/queryOpenState", lockId=2)

        assert any(
            record.name == "custom_components.ttlock.device.1"
            for record in caplog.records
        )
        assert not any(
            record.name == "custom_components.ttlock.device.2"
            for record in caplog.records
        )

    async def test_request_scoped_to_lock_id_logs_via_that_locks_child_logger(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker, caplog
    ):
        mocker.get(f"{BASE}lock/detail", json={"errcode": 0})
        caplog.set_level(logging.DEBUG, logger="custom_components.ttlock.device.42")

        await ttlock_api.get("lock/detail", lockId=42)

        assert any(
            "Sending request" in record.message
            and record.name == "custom_components.ttlock.device.42"
            for record in caplog.records
        )

    async def test_request_without_lock_id_logs_via_shared_logger(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker, caplog
    ):
        mocker.get(f"{BASE}lock/list", json={"list": []})
        caplog.set_level(logging.DEBUG, logger="custom_components.ttlock.api")

        await ttlock_api.get_locks()

        assert any(
            record.name == "custom_components.ttlock.api" for record in caplog.records
        )


class TestDebugCapture:
    async def test_lock_scoped_request_is_captured_without_any_logger_opt_in(
        self, mock_oauth_session, mocker: AiohttpClientMocker
    ):
        """Capture is always-on: no caplog/logger level is touched here at all."""
        mocker.get(f"{BASE}lock/detail", json={"errcode": 0})

        capture = LockTrafficCapture()
        session = mocker.create_session(None)
        try:
            api = TTLockApi(session, mock_oauth_session, capture)
            await api.get("lock/detail", lockId=42)
        finally:
            await session.close()

        captured = capture.diagnostics_for(42)
        assert captured["window"] is not None
        assert any(
            "Sending request" in record["message"] for record in captured["records"]
        )
        assert any(
            "Received response" in record["message"] for record in captured["records"]
        )

    async def test_request_without_lock_id_is_not_captured(
        self, mock_oauth_session, mocker: AiohttpClientMocker
    ):
        """Account-wide (non-lock-scoped) calls have nothing to key a buffer on."""
        mocker.get(f"{BASE}lock/list", json={"list": []})

        capture = LockTrafficCapture()
        session = mocker.create_session(None)
        try:
            api = TTLockApi(session, mock_oauth_session, capture)
            await api.get_locks()
        finally:
            await session.close()

        assert capture.diagnostics_for(42)["records"] == []


class TestGetLocks:
    async def test_returns_all_locks_as_summaries(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        # get_locks() no longer filters by connectability - that's now
        # LockSummary.connectable, exercised directly in test_models.py.
        # Non-connectable locks still get entities (shown unavailable).
        mocker.get(
            f"{BASE}lock/list",
            json={
                "list": [
                    {
                        "lockId": 1,
                        "lockAlias": "Front Door",
                        "lockMac": "00:00:00:00:00:01",
                        "hasGateway": 1,
                        "featureValue": "0",
                    },
                    {
                        "lockId": 2,
                        "lockAlias": "Shed",
                        "lockMac": "00:00:00:00:00:02",
                        "hasGateway": 0,
                        "featureValue": "0",
                    },
                ]
            },
        )

        locks = await ttlock_api.get_locks()

        assert [lock.id for lock in locks] == [1, 2]
        assert locks[0].connectable is True
        assert locks[1].connectable is False


class TestGetSensor:
    async def test_returns_sensor_when_present(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(
            f"{BASE}doorSensor/query",
            json={
                "doorSensorId": 1,
                "name": "Sensor",
                "electricQuantity": 90,
                "mac": "00:00:00:00:00:00",
            },
        )

        sensor = await ttlock_api.get_sensor(1)

        assert sensor is not None
        assert sensor.battery_level == 90

    async def test_returns_none_when_request_fails(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}doorSensor/query", json={"errcode": 1, "errmsg": "nope"})

        assert await ttlock_api.get_sensor(1) is None


class TestLockUnlock:
    async def test_lock_succeeds(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lock/lock", json={"errcode": 0})

        assert await ttlock_api.lock(1) is True

    async def test_lock_fails(self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker):
        # _parse_resp raises RequestFailed for any non-zero errcode before
        # lock() ever sees the response, so a failure surfaces as an
        # exception rather than a `False` return.
        mocker.get(f"{BASE}lock/lock", json={"errcode": 1, "errmsg": "jammed"})

        with pytest.raises(RequestFailed):
            await ttlock_api.lock(1)

    async def test_unlock_succeeds(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lock/unlock", json={"errcode": 0})

        assert await ttlock_api.unlock(1) is True

    async def test_unlock_fails(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lock/unlock", json={"errcode": 1, "errmsg": "jammed"})

        with pytest.raises(RequestFailed):
            await ttlock_api.unlock(1)


class TestSetPassageMode:
    async def test_sends_expected_params(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}lock/configPassageMode", json={"errcode": 0})
        config = PassageModeConfig.model_validate(PASSAGE_MODE_6_TO_6_7_DAYS)

        assert await ttlock_api.set_passage_mode(1, config) is True

        data = mocker.mock_calls[0][2]
        assert data["passageMode"] == 1
        assert data["autoUnlock"] == 2
        assert data["isAllDay"] == 2
        assert data["startDate"] == config.start_minute
        assert data["endDate"] == config.end_minute

    async def test_reports_failure(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(
            f"{BASE}lock/configPassageMode", json={"errcode": 1, "errmsg": "no"}
        )
        config = PassageModeConfig.model_validate(PASSAGE_MODE_6_TO_6_7_DAYS)

        with pytest.raises(RequestFailed):
            await ttlock_api.set_passage_mode(1, config)


class TestAddPasscode:
    async def test_permanent_passcode_omits_dates(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}keyboardPwd/add", json={"errcode": 0})
        config = AddPasscodeConfig(passcode="1234", passcodeName="Guest")

        assert await ttlock_api.add_passcode(1, config) is True

        data = mocker.mock_calls[0][2]
        assert data["keyboardPwdType"] == 2
        assert "startDate" not in data
        assert "endDate" not in data

    async def test_temporary_passcode_includes_dates(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}keyboardPwd/add", json={"errcode": 0})
        config = AddPasscodeConfig(
            passcode="1234", passcodeName="Guest", startDate=10, endDate=20
        )

        assert await ttlock_api.add_passcode(1, config) is True

        data = mocker.mock_calls[0][2]
        assert data["keyboardPwdType"] == 3
        assert data["startDate"] == 10
        assert data["endDate"] == 20

    async def test_reports_failure(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}keyboardPwd/add", json={"errcode": 1, "errmsg": "full"})
        config = AddPasscodeConfig(passcode="1234", passcodeName="Guest")

        with pytest.raises(RequestFailed):
            await ttlock_api.add_passcode(1, config)


class TestModifyPasscode:
    async def test_only_sends_changed_fields(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}keyboardPwd/change", json={"errcode": 0})
        config = AddPasscodeConfig(passcodeName="New Name")

        assert await ttlock_api.modify_passcode(1, 99, config) is True

        data = mocker.mock_calls[0][2]
        assert data["keyboardPwdName"] == "New Name"
        assert "newKeyboardPwd" not in data
        assert "startDate" not in data
        assert "endDate" not in data

    async def test_reports_failure(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(
            f"{BASE}keyboardPwd/change", json={"errcode": 1, "errmsg": "bad id"}
        )
        config = AddPasscodeConfig(passcodeName="New Name")

        with pytest.raises(RequestFailed):
            await ttlock_api.modify_passcode(1, 99, config)


class TestListAndDeletePasscode:
    async def test_list_passcodes(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(
            f"{BASE}lock/listKeyboardPwd",
            json={
                "list": [
                    {"keyboardPwdId": 1, "keyboardPwd": "1234"},
                    {"keyboardPwdId": 2, "keyboardPwd": "5678"},
                ]
            },
        )

        passcodes = await ttlock_api.list_passcodes(1)

        assert [p.id for p in passcodes] == [1, 2]

    async def test_delete_passcode_succeeds(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}keyboardPwd/delete", json={"errcode": 0})

        assert await ttlock_api.delete_passcode(1, 99) is True

    async def test_delete_passcode_fails(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(
            f"{BASE}keyboardPwd/delete", json={"errcode": 1, "errmsg": "bad id"}
        )

        with pytest.raises(RequestFailed):
            await ttlock_api.delete_passcode(1, 99)


class TestAutoLockAndSound:
    async def test_set_auto_lock_succeeds(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}lock/setAutoLockTime", json={"errcode": 0})

        assert await ttlock_api.set_auto_lock(1, 30) is True

    async def test_set_auto_lock_fails(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}lock/setAutoLockTime", json={"errcode": 1, "errmsg": "no"})

        with pytest.raises(RequestFailed):
            await ttlock_api.set_auto_lock(1, 30)

    async def test_set_lock_sound_succeeds(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}lock/updateSetting", json={"errcode": 0})

        assert await ttlock_api.set_lock_sound(1, 1) is True

    async def test_set_lock_sound_fails(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.post(f"{BASE}lock/updateSetting", json={"errcode": 1, "errmsg": "no"})

        with pytest.raises(RequestFailed):
            await ttlock_api.set_lock_sound(1, 1)


class TestGetLockRecords:
    async def test_omits_optional_dates_and_caps_page_size(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lockRecord/list", json={"list": []})

        await ttlock_api.get_lock_records(1, page_size=1000)

        query = mocker.mock_calls[0][1].query
        assert "startDate" not in query
        assert "endDate" not in query
        assert query["pageSize"] == "200"

    async def test_includes_dates_when_provided(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lockRecord/list", json={"list": []})

        await ttlock_api.get_lock_records(1, start_date=100, end_date=200)

        query = mocker.mock_calls[0][1].query
        assert query["startDate"] == "100"
        assert query["endDate"] == "200"

    async def test_parses_records(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(
            f"{BASE}lockRecord/list",
            json={
                "list": [
                    {
                        "recordId": 1,
                        "lockId": 1,
                        "recordType": 7,
                        "success": 1,
                        "username": "test",
                        "lockDate": 1621459200000,
                        "serverDate": 1621459200000,
                    }
                ]
            },
        )

        records = await ttlock_api.get_lock_records(1)

        assert len(records) == 1
        assert records[0].id == 1


class TestGetLockAndGateways:
    async def test_get_lock(self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker):
        mocker.get(f"{BASE}lock/detail", json=BASIC_LOCK_DETAILS)

        lock = await ttlock_api.get_lock(BASIC_LOCK_DETAILS["lockId"])  # ty: ignore[invalid-argument-type] - dict value type widened by other fields, this key is always an int

        assert lock.id == BASIC_LOCK_DETAILS["lockId"]
        assert lock.name == BASIC_LOCK_DETAILS["lockAlias"]

    async def test_get_gateways(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(
            f"{BASE}gateway/list",
            json={
                "list": [
                    {
                        "gatewayId": 1,
                        "gatewayName": "GW",
                        "gatewayMac": "00:00:00:00:00:00",
                        "isOnline": 1,
                    }
                ]
            },
        )

        gateways = await ttlock_api.get_gateways()

        assert len(gateways) == 1
        assert gateways[0].id == 1

    async def test_get_lock_passage_mode_config(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(f"{BASE}lock/getPassageModeConfig", json=PASSAGE_MODE_6_TO_6_7_DAYS)

        config = await ttlock_api.get_lock_passage_mode_config(1)

        assert config.enabled == OnOff.on

    async def test_get_lock_state(
        self, ttlock_api: TTLockApi, mocker: AiohttpClientMocker
    ):
        mocker.get(
            f"{BASE}lock/queryOpenState",
            json={"state": 0, "electricQuantity": 90, "lockTime": 1621459200000},
        )

        state = await ttlock_api.get_lock_state(1)

        assert state.locked is not None
        assert state.locked.value == 0


class TestTTLockAuthImplementation:
    @pytest.fixture
    def implementation(self, hass: HomeAssistant) -> TTLockAuthImplementation:
        return TTLockAuthImplementation(
            hass,
            "ttlock",
            ClientCredential("client-id", "client-secret"),
            AuthorizationServer(
                authorize_url="https://euapi.ttlock.com/oauth2/authorize",
                token_url="https://euapi.ttlock.com/oauth2/token",
            ),
        )

    async def test_login_hashes_password_and_makes_token_request(
        self, implementation: TTLockAuthImplementation
    ):
        with patch.object(
            implementation, "_token_request", new_callable=AsyncMock
        ) as mock_token_request:
            mock_token_request.return_value = {"access_token": "abc"}

            result = await implementation.login("jonas", "hunter2")

        assert result == {"access_token": "abc"}
        mock_token_request.assert_awaited_once_with(
            {
                "username": "jonas",
                "password": "2ab96390c7dbe3439de74d0c9b0b1767",
            }
        )

    async def test_async_refresh_token_merges_new_token(
        self, implementation: TTLockAuthImplementation
    ):
        with patch.object(
            implementation, "_token_request", new_callable=AsyncMock
        ) as mock_token_request:
            mock_token_request.return_value = {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
            }

            result = await implementation._async_refresh_token(
                {"refresh_token": "old-refresh-token", "extra": "kept"}
            )

        assert result == {
            "extra": "kept",
            "refresh_token": "new-refresh-token",
            "access_token": "new-access-token",
        }
        mock_token_request.assert_awaited_once_with(
            {
                "grant_type": "refresh_token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "old-refresh-token",
            }
        )

    async def test_async_resolve_external_data_returns_dict_copy(
        self, implementation: TTLockAuthImplementation
    ):
        external_data = {"foo": "bar"}

        result = await implementation.async_resolve_external_data(external_data)

        assert result == external_data
        assert result is not external_data


# The tests below exercise the card/fingerprint method bodies with only the
# HTTP layer (get/post) mocked via AsyncMock on the api fixture, rather than
# the full aiohttp-level mocking used above - lighter weight since these
# methods don't add any request-building logic beyond get()/post() themselves.


class TestListCards:
    async def test_parses_cards_and_requests_all_pages(self, api):
        api.get = AsyncMock(
            return_value={
                "list": [
                    {
                        "cardId": 124242,
                        "lockId": 163377,
                        "cardNumber": "1723612378",
                        "cardName": "Card for mom",
                        "cardType": 1,
                        "senderUsername": "alexa@google.com",
                        "startDate": 0,
                        "endDate": 0,
                    }
                ]
            }
        )

        cards = await api.list_cards(163377)

        api.get.assert_awaited_once_with(
            "identityCard/list",
            lockId=163377,
            pageNo=1,
            pageSize=200,
            orderBy=1,
        )
        assert len(cards) == 1
        assert isinstance(cards[0], Card)
        assert cards[0].id == 124242
        assert cards[0].type == CardType.normal
        # startDate/endDate == 0 means permanent -> no dates, never expired.
        assert cards[0].start_date is None
        assert cards[0].end_date is None
        assert cards[0].expired is False

    async def test_empty_list(self, api):
        api.get = AsyncMock(return_value={"list": []})
        assert await api.list_cards(1) == []


class TestRenameCard:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.rename_card(163377, 124242, "New name") is True
        api.post.assert_awaited_once_with(
            "identityCard/rename",
            lockId=163377,
            cardId=124242,
            cardName="New name",
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.rename_card(163377, 124242, "New name") is False


class TestDeleteCard:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.delete_card(163377, 124242) is True
        api.post.assert_awaited_once_with(
            "identityCard/delete",
            lockId=163377,
            cardId=124242,
            deleteType=2,
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.delete_card(163377, 124242) is False


class TestListFingerprints:
    async def test_parses_fingerprints_and_requests_all_pages(self, api):
        api.get = AsyncMock(
            return_value={
                "list": [
                    {
                        "fingerprintId": 224242,
                        "lockId": 163377,
                        "fingerprintNumber": "44668054142981",
                        "fingerprintName": "Thumb",
                        "fingerprintType": 1,
                        "senderUsername": "alexa@google.com",
                        "startDate": 0,
                        "endDate": 0,
                    }
                ]
            }
        )

        prints = await api.list_fingerprints(163377)

        api.get.assert_awaited_once_with(
            "fingerprint/list",
            lockId=163377,
            pageNo=1,
            pageSize=200,
            orderBy=1,
        )
        assert len(prints) == 1
        assert isinstance(prints[0], Fingerprint)
        assert prints[0].id == 224242
        assert prints[0].name == "Thumb"
        assert prints[0].start_date is None
        assert prints[0].end_date is None

    async def test_empty_list(self, api):
        api.get = AsyncMock(return_value={"list": []})
        assert await api.list_fingerprints(1) == []


class TestRenameFingerprint:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.rename_fingerprint(163377, 224242, "Left thumb") is True
        api.post.assert_awaited_once_with(
            "fingerprint/rename",
            lockId=163377,
            fingerprintId=224242,
            fingerprintName="Left thumb",
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.rename_fingerprint(163377, 224242, "Left thumb") is False


class TestDeleteFingerprint:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.delete_fingerprint(163377, 224242) is True
        api.post.assert_awaited_once_with(
            "fingerprint/delete",
            lockId=163377,
            fingerprintId=224242,
            deleteType=2,
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.delete_fingerprint(163377, 224242) is False


@pytest.mark.parametrize("method", ["list_cards", "list_fingerprints"])
async def test_list_methods_propagate_missing_list_key(api, method):
    """A malformed response without a `list` key should raise, not silently pass."""
    api.get = AsyncMock(return_value={})
    with pytest.raises(KeyError):
        await getattr(api, method)(1)
