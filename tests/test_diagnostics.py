"""Test ttlock diagnostics."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ttlock.api import TTLockApi
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import DOMAIN, TT_GATEWAYS, TT_LOCKS
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.ttlock.models import Gateway, LockSummary
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import BASIC_LOCK_DETAILS, LOCK_STATE_UNLOCKED, PASSAGE_MODE_6_TO_6_7_DAYS

API_BASE = "https://euapi.ttlock.com/v3/"


def _mock_connectable_and_gatewayless_lock(monkeypatch) -> None:
    """Patch get_locks to return one connectable lock and one without a gateway."""

    async def mock_get_locks(*args, **kwargs):
        return [
            LockSummary(
                lockId=7252408,
                lockAlias="Connectable Lock",
                lockMac="00:00:00:00:00:01",
                hasGateway=1,
            ),
            LockSummary(
                lockId=2,
                lockAlias="No Gateway Lock",
                lockMac="00:00:00:00:00:02",
                hasGateway=0,
            ),
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
    )


async def test_diagnostics_includes_connectable_and_health(
    hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
):
    """Diagnostics surface connectable and last_update_success for troubleshooting."""
    mock_api_responses("default")
    _mock_connectable_and_gatewayless_lock(monkeypatch)

    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    locks_by_id = {lock["unique_id"]: lock for lock in diagnostics["locks"]}
    assert locks_by_id[f"{DOMAIN}-7252408"]["connectable"] is True
    assert locks_by_id[f"{DOMAIN}-7252408"]["last_update_success"] is True
    # non-connectable coordinators are never refreshed at all (update_interval=None,
    # excluded from the background detail fill), so last_update_success just sits at
    # its untouched default - `connectable` is the signal to look at for these.
    assert locks_by_id[f"{DOMAIN}-2"]["connectable"] is False


async def test_diagnostics_redacts_oauth_token(
    hass: HomeAssistant, component_setup, mock_api_responses
):
    """The config entry's OAuth access/refresh tokens must never appear in the clear."""
    mock_api_responses("default")
    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    token = diagnostics["config_entry"]["data"]["token"]
    assert token == "**REDACTED**"


async def test_diagnostics_includes_raw_connectivity_fields_and_gateway_status(
    hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
):
    """Diagnostics surface the raw lock-list fields behind Connectable, plus gateway status."""
    mock_api_responses("default")

    async def mock_get_locks(*args, **kwargs):
        return [
            LockSummary(
                lockId=2,
                lockAlias="No Gateway Lock",
                lockMac="00:00:00:00:00:02",
                hasGateway=0,
                featureValue="000000",
            ),
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
    )

    async def mock_get_gateways(*args, **kwargs):
        return [
            Gateway.model_validate(
                {
                    "gatewayId": 1461158,
                    "gatewayName": "Test Gateway",
                    "gatewayMac": "05:F6:1E:93:8B:2B",
                    "isOnline": 1,
                }
            )
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_gateways", mock_get_gateways
    )

    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    lock = diagnostics["locks"][0]
    assert lock["connectable"] is False
    assert lock["has_gateway"] == 0
    assert lock["feature_value"] == "000000"

    gateways = diagnostics["gateways"]
    assert gateways[0]["name"] == "Test Gateway"
    assert gateways[0]["is_online"] is True


async def test_device_diagnostics_matches_config_entry_lock_entry(
    hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
):
    """Device-scoped diagnostics return only the requested lock, matching the config-entry dump."""
    mock_api_responses("default")
    _mock_connectable_and_gatewayless_lock(monkeypatch)

    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, "00:00:00:00:00:02")}
    )
    assert device is not None

    device_diagnostics = await async_get_device_diagnostics(hass, entry, device)
    config_entry_diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    expected = next(
        lock
        for lock in config_entry_diagnostics["locks"]
        if lock["unique_id"] == f"{DOMAIN}-2"
    )
    assert device_diagnostics == expected
    assert {lock["unique_id"] for lock in config_entry_diagnostics["locks"]} == {
        f"{DOMAIN}-2",
        f"{DOMAIN}-7252408",
    }


async def test_debug_capture_appears_redacted_in_diagnostics(
    hass: HomeAssistant, caplog
):
    """A lock's raw traffic is captured into diagnostics unconditionally - no logger
    opt-in required - and redacted independently of HA's live logs.

    Deliberately bypasses the `mock_api_responses` fixture, which stubs out
    TTLockApi's methods entirely and so never exercises the request/response
    debug logging this test is about - instead it mocks at the HTTP layer
    (like test_api.py) so the real TTLockApi.get/_parse_resp logging runs.
    """
    device_logger_name = "custom_components.ttlock.device.7252408"
    # pytest_homeassistant_custom_component forces the root logger to DEBUG
    # for every test (see the equivalent note in test_api.py), so this lock's
    # logger needs an explicit level above DEBUG to simulate its normal,
    # un-enabled default - proving capture doesn't depend on it.
    caplog.set_level(logging.WARNING, logger=device_logger_name)

    mocker = AiohttpClientMocker()
    mocker.get(f"{API_BASE}lock/detail", json=BASIC_LOCK_DETAILS)
    mocker.get(f"{API_BASE}lock/queryOpenState", json=LOCK_STATE_UNLOCKED)
    mocker.get(f"{API_BASE}lock/getPassageModeConfig", json=PASSAGE_MODE_6_TO_6_7_DAYS)

    oauth_session = MagicMock()
    oauth_session.valid_token = True
    oauth_session.token = {"access_token": "mock-access-token"}
    oauth_session.implementation.client_id = "mock-client-id"
    oauth_session.async_ensure_token_valid = AsyncMock()

    session = mocker.create_session(None)
    capture = LockTrafficCapture()

    try:
        api = TTLockApi(session, oauth_session, capture)
        config_entry = MockConfigEntry(domain=DOMAIN)
        config_entry.add_to_hass(hass)
        summary = LockSummary(
            lockId=7252408,
            lockAlias="Front Door",
            lockMac="16:72:4C:CC:01:C4",
            hasGateway=1,
        )
        coordinator = LockUpdateCoordinator(hass, config_entry, api, summary, capture)
        await coordinator.async_refresh()

        hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {
            TT_LOCKS: [coordinator],
            TT_GATEWAYS: SimpleNamespace(as_dict=list),
        }

        device_registry = dr.async_get(hass)
        device = device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id, **coordinator.device_info
        )

        diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
        device_diagnostics = await async_get_device_diagnostics(
            hass, config_entry, device
        )
    finally:
        await session.close()

    capture_result = diagnostics["locks"][0]["debug_capture"]
    assert capture_result["window"] is not None

    # Per-device diagnostics carry the same capture content, not just the
    # config-entry-level dump - both surfaces are required.
    assert device_diagnostics["debug_capture"] == capture_result

    captured_messages = [record["message"] for record in capture_result["records"]]
    assert any("Received response" in message for message in captured_messages)

    # lockKey is a known-sensitive field present in the real lock/detail response body.
    sensitive_value = BASIC_LOCK_DETAILS["lockKey"]
    assert not any(sensitive_value in message for message in captured_messages)
    assert any("**REDACTED**" in message for message in captured_messages)

    # This lock's logger was never raised to DEBUG, so HA's live log output
    # stays silent - capture is independent of, not gated by, that opt-in.
    assert not any(record.name == device_logger_name for record in caplog.records)
