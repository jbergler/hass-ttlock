"""Test the TTLock webhook handler."""

import json
import logging
from typing import cast
from unittest.mock import ANY, AsyncMock, patch

from aiohttp.web import Request
from multidict import MultiDict
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    return WebhookHandler(hass, entry)


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

    async def test_dismisses_setup_message_on_first_success(
        self, hass: HomeAssistant, handler: WebhookHandler, entry: MockConfigEntry
    ):
        assert CONF_WEBHOOK_STATUS not in entry.data

        with patch.object(handler, "async_dismiss_setup_message") as mock_dismiss:
            request = _request(
                MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])})
            )
            await handler.handle_webhook(hass, "wh-id", request)

        mock_dismiss.assert_called_once()

    async def test_does_not_dismiss_setup_message_when_already_dismissed(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_STATUS: True}
        )
        handler = WebhookHandler(hass, entry)

        with patch.object(handler, "async_dismiss_setup_message") as mock_dismiss:
            request = _request(
                MultiDict({"records": json.dumps([WEBHOOK_LOCK_10AM_UTC])})
            )
            await handler.handle_webhook(hass, "wh-id", request)

        mock_dismiss.assert_not_called()

    async def test_does_not_dismiss_setup_message_without_records(
        self, hass: HomeAssistant, handler: WebhookHandler
    ):
        with patch.object(handler, "async_dismiss_setup_message") as mock_dismiss:
            await handler.handle_webhook(hass, "wh-id", _request(MultiDict()))

        mock_dismiss.assert_not_called()


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


class TestDismissSetupMessage:
    async def test_marks_status_and_dismisses_notification(
        self, hass: HomeAssistant, handler: WebhookHandler, entry: MockConfigEntry
    ):
        assert CONF_WEBHOOK_STATUS not in entry.data

        with patch(
            "homeassistant.components.persistent_notification.async_dismiss"
        ) as mock_dismiss:
            handler.async_dismiss_setup_message()

        assert entry.data[CONF_WEBHOOK_STATUS] is True
        mock_dismiss.assert_called_once_with(hass, entry.entry_id)
