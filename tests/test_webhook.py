"""Test the TTLock webhook handler."""

import json
import logging
from typing import cast
from unittest.mock import ANY, AsyncMock, patch

from aiohttp.web import Request
from multidict import MultiDict
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_WEBHOOK_STATUS,
    CONF_WEBHOOK_URL,
    DOMAIN,
    SIGNAL_NEW_DATA,
)
from custom_components.ttlock.webhook import WebhookHandler
from homeassistant.const import CONF_WEBHOOK_ID, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import WEBHOOK_LOCK_10AM_UTC, WEBHOOK_UNLOCK_10AM_UTC


def _mocked_entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    """A config entry authenticated via the "mocked" client-id credential."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"auth_implementation": "mocked", **data}
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def credential(hass: HomeAssistant, multi_account_credential) -> None:
    """Register the shared client-id credential these tests' entries use."""
    await multi_account_credential(hass)


class FakeRequest:
    """Minimal stand-in for aiohttp's web Request."""

    def __init__(self, post_data=None, text: str = ""):
        self._post_data = post_data if post_data is not None else MultiDict()
        self._text = text

    async def post(self):
        return self._post_data

    async def text(self):
        return self._text


def _request(post_data=None, text: str = "") -> Request:
    """Build a FakeRequest, typed as Request for handle_webhook's signature."""
    return cast(Request, FakeRequest(post_data, text))


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    config_entry = MockConfigEntry(domain=DOMAIN, data={CONF_WEBHOOK_ID: "wh-id"})
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
def handler(hass: HomeAssistant, entry: MockConfigEntry) -> WebhookHandler:
    return WebhookHandler(hass, entry, client_id="client-id")


class TestHandleWebhook:
    async def test_dispatches_parsed_event_for_each_record(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        received = []
        async_dispatcher_connect(hass, SIGNAL_NEW_DATA, received.append)

        request = _request(MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])}))
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        assert len(received) == 1
        assert received[0].id == WEBHOOK_LOCK_10AM_UTC["lockId"]

    async def test_dispatches_event_for_every_records_field(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        received = []
        async_dispatcher_connect(hass, SIGNAL_NEW_DATA, received.append)

        data = MultiDict()
        data.add("records", json.dumps([WEBHOOK_LOCK_10AM_UTC]))
        data.add("records", json.dumps([WEBHOOK_UNLOCK_10AM_UTC]))

        await handler.handle_webhook(hass, "wh-id", _request(data))
        await hass.async_block_till_done()

        assert len(received) == 2

    async def test_ignores_malformed_json(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        received = []
        async_dispatcher_connect(hass, SIGNAL_NEW_DATA, received.append)

        request = _request(MultiDict({"records": "not-json"}))
        # Should not raise, just log and return.
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        assert received == []

    async def test_handles_empty_payload(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        received = []
        async_dispatcher_connect(hass, SIGNAL_NEW_DATA, received.append)

        request = _request(MultiDict(), text="")
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        assert received == []

    async def test_resolves_setup_issue_on_first_success(
        self, hass: HomeAssistant, handler: WebhookHandler, entry: MockConfigEntry
    ):
        assert CONF_WEBHOOK_STATUS not in entry.data

        with patch.object(handler, "async_resolve_setup_issue") as mock_resolve:
            request = _request(
                MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])})
            )
            await handler.handle_webhook(hass, "wh-id", request)

        mock_resolve.assert_called_once()

    async def test_does_not_resolve_setup_issue_when_already_resolved(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_STATUS: True}
        )
        handler = WebhookHandler(hass, entry)

        with patch.object(handler, "async_resolve_setup_issue") as mock_resolve:
            request = _request(
                MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])})
            )
            await handler.handle_webhook(hass, "wh-id", request)

        mock_resolve.assert_not_called()

    async def test_does_not_resolve_setup_issue_without_records(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        with patch.object(handler, "async_resolve_setup_issue") as mock_resolve:
            await handler.handle_webhook(hass, "wh-id", _request(MultiDict()))

        mock_resolve.assert_not_called()


class TestPerLockLogging:
    async def test_debug_on_one_locks_logger_does_not_capture_anothers_webhook_data(
        self, hass: HomeAssistant, handler: WebhookHandler, caplog
    ):
        # See the equivalent test in tests/test_api.py for why the WARNING
        # call must come first: caplog.set_level shares one handler whose
        # level is overwritten by each call.
        caplog.set_level(logging.WARNING, logger="custom_components.ttlock.device.2")
        caplog.set_level(
            logging.DEBUG, logger="custom_components.ttlock.device.7252408"
        )

        request = _request(
            MultiDict(
                {"lockId": "7252408", "records": json.dumps([WEBHOOK_LOCK_10AM_UTC])}
            )
        )
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        assert any(
            record.name == "custom_components.ttlock.device.7252408"
            for record in caplog.records
        )
        assert not any(
            record.name == "custom_components.ttlock.device.2"
            for record in caplog.records
        )

    async def test_webhook_data_without_lock_id_logs_via_shared_logger(
        self, hass: HomeAssistant, handler: WebhookHandler, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="custom_components.ttlock.webhook")

        request = _request(MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])}))
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        assert any(
            record.name == "custom_components.ttlock.webhook"
            for record in caplog.records
        )


class TestDebugCapture:
    async def test_lock_scoped_webhook_data_is_captured_without_any_logger_opt_in(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ):
        """Capture is always-on: no caplog/logger level is touched here at all."""
        capture = LockTrafficCapture()
        handler = WebhookHandler(hass, entry, capture)

        request = _request(
            MultiDict(
                {"lockId": "7252408", "records": json.dumps([WEBHOOK_LOCK_10AM_UTC])}
            )
        )
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        captured = capture.diagnostics_for(7252408)
        assert captured["window"] is not None
        assert any(
            "Got webhook data" in record["message"] for record in captured["records"]
        )

    async def test_webhook_data_without_lock_id_is_not_captured(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ):
        """Account-wide (non-lock-scoped) webhook payloads have nothing to key a buffer on."""
        capture = LockTrafficCapture()
        handler = WebhookHandler(hass, entry, capture)

        request = _request(MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])}))
        await handler.handle_webhook(hass, "wh-id", request)
        await hass.async_block_till_done()

        assert capture.diagnostics_for(7252408)["records"] == []


class TestSetup:
    async def test_registers_immediately_when_hass_running(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        assert hass.state is CoreState.running

        with patch.object(
            handler, "register_webhook", new_callable=AsyncMock
        ) as mock_register:
            await handler.setup()

        mock_register.assert_awaited_once_with()

    async def test_waits_for_started_event_when_not_running(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        hass.set_state(CoreState.not_running)

        with patch.object(
            handler, "register_webhook", new_callable=AsyncMock
        ) as mock_register:
            await handler.setup()
            mock_register.assert_not_called()

            hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
            await hass.async_block_till_done()

        mock_register.assert_awaited_once_with(ANY)


class TestGetUrl:
    async def test_returns_cached_url_without_generating(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_URL: "https://cached.example/hook"}
        )
        handler = WebhookHandler(hass, entry)

        with patch.object(
            handler, "try_generate_cloudhook", new_callable=AsyncMock
        ) as mock_cloudhook:
            url = await handler.get_url()

        assert url == "https://cached.example/hook"
        mock_cloudhook.assert_not_called()


class TestResolveSetupIssue:
    async def test_marks_status_and_resolves_issue(
        self, hass: HomeAssistant, handler: WebhookHandler, entry: MockConfigEntry
    ):
        assert CONF_WEBHOOK_STATUS not in entry.data

        with patch(
            "homeassistant.helpers.issue_registry.async_delete_issue"
        ) as mock_delete:
            handler.async_resolve_setup_issue()

        assert entry.data[CONF_WEBHOOK_STATUS] is True
        mock_delete.assert_called_once_with(hass, DOMAIN, "webhook_setup_client-id")


class TestResolveGroupWebhookId:
    """Reconciling config entries that share one client_id's webhook."""

    async def test_mints_new_id_when_group_has_none(
        self, hass: HomeAssistant, credential: None
    ):
        entry = _mocked_entry(hass)
        handler = WebhookHandler(hass, entry, client_id="client-id")

        webhook_id, confirmed = await handler._resolve_group_webhook_id()

        assert webhook_id
        assert confirmed is False
        assert entry.data[CONF_WEBHOOK_ID] == webhook_id

    async def test_single_existing_value_has_no_ambiguity(
        self, hass: HomeAssistant, credential: None
    ):
        entry_a = _mocked_entry(hass, **{CONF_WEBHOOK_ID: "wh-a"})
        entry_b = _mocked_entry(hass)
        handler = WebhookHandler(hass, entry_a, client_id="client-id")

        with patch(
            "homeassistant.helpers.issue_registry.async_create_issue"
        ) as mock_issue:
            webhook_id, confirmed = await handler._resolve_group_webhook_id()

        assert webhook_id == "wh-a"
        assert confirmed is False
        assert entry_b.data[CONF_WEBHOOK_ID] == "wh-a"
        mock_issue.assert_not_called()

    async def test_confirmed_entry_wins_when_values_diverge(
        self, hass: HomeAssistant, credential: None
    ):
        entry_a = _mocked_entry(
            hass, **{CONF_WEBHOOK_ID: "wh-a", CONF_WEBHOOK_STATUS: True}
        )
        entry_b = _mocked_entry(hass, **{CONF_WEBHOOK_ID: "wh-b"})
        handler = WebhookHandler(hass, entry_a, client_id="client-id")

        with patch(
            "homeassistant.helpers.issue_registry.async_create_issue"
        ) as mock_issue:
            webhook_id, confirmed = await handler._resolve_group_webhook_id()

        assert webhook_id == "wh-a"
        assert confirmed is True
        assert entry_b.data[CONF_WEBHOOK_ID] == "wh-a"
        assert entry_b.data[CONF_WEBHOOK_STATUS] is True
        mock_issue.assert_not_called()

    async def test_multiple_entries_confirming_the_same_value_has_no_ambiguity(
        self, hass: HomeAssistant, credential: None
    ):
        """Two confirmed entries that already agree aren't "ambiguous" just
        because more than one of them is confirmed.
        """
        entry_a = _mocked_entry(
            hass, **{CONF_WEBHOOK_ID: "wh-a", CONF_WEBHOOK_STATUS: True}
        )
        entry_b = _mocked_entry(
            hass, **{CONF_WEBHOOK_ID: "wh-a", CONF_WEBHOOK_STATUS: True}
        )
        entry_c = _mocked_entry(hass, **{CONF_WEBHOOK_ID: "wh-c"})
        handler = WebhookHandler(hass, entry_a, client_id="client-id")

        with patch(
            "homeassistant.helpers.issue_registry.async_create_issue"
        ) as mock_issue:
            webhook_id, confirmed = await handler._resolve_group_webhook_id()

        assert webhook_id == "wh-a"
        assert confirmed is True
        assert entry_b.data[CONF_WEBHOOK_ID] == "wh-a"
        assert entry_c.data[CONF_WEBHOOK_ID] == "wh-a"
        assert entry_c.data[CONF_WEBHOOK_STATUS] is True
        mock_issue.assert_not_called()

    async def test_ambiguous_values_fall_back_to_entry_order_and_raise_repair_issue(
        self, hass: HomeAssistant, credential: None
    ):
        entry_a = _mocked_entry(hass, **{CONF_WEBHOOK_ID: "wh-a"})
        entry_b = _mocked_entry(hass, **{CONF_WEBHOOK_ID: "wh-b"})
        handler = WebhookHandler(hass, entry_a, client_id="client-id")

        with patch(
            "homeassistant.helpers.issue_registry.async_create_issue"
        ) as mock_issue:
            webhook_id, confirmed = await handler._resolve_group_webhook_id()

        assert webhook_id == "wh-a"
        assert confirmed is False
        assert entry_b.data[CONF_WEBHOOK_ID] == "wh-a"
        mock_issue.assert_called_once()
        assert (
            mock_issue.call_args.args[2] == "webhook_consolidation_ambiguous_client-id"
        )
        # The current webhook URL is embedded directly in the issue - it
        # can't just point at the "TTLock Setup" notification, since that
        # gets dismissed the moment traffic arrives while the repair issue
        # stays open regardless.
        placeholders = mock_issue.call_args.kwargs["translation_placeholders"]
        assert "wh-a" in placeholders["webhook_url"]

    async def test_ambiguous_guess_matching_a_conflicting_confirmed_id_is_not_confirmed(
        self, hass: HomeAssistant, credential: None
    ):
        """A guess isn't "confirmed" just because it happens to equal one of
        several conflicting previously-confirmed ids - by definition we
        can't trust it, and marking it confirmed would suppress the setup
        notification carrying the guessed URL.
        """
        entry_a = _mocked_entry(
            hass, **{CONF_WEBHOOK_ID: "wh-a", CONF_WEBHOOK_STATUS: True}
        )
        _mocked_entry(hass, **{CONF_WEBHOOK_ID: "wh-b", CONF_WEBHOOK_STATUS: True})
        handler = WebhookHandler(hass, entry_a, client_id="client-id")

        with patch("homeassistant.helpers.issue_registry.async_create_issue"):
            webhook_id, confirmed = await handler._resolve_group_webhook_id()

        # "wh-a" (entry_a's own id) wins the entry-order fallback and is
        # also, coincidentally, one of the two conflicting confirmed ids -
        # that coincidence must not be read as "the group is confirmed".
        assert webhook_id == "wh-a"
        assert confirmed is False
