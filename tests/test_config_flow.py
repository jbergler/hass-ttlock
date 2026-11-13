"""Test the TTLock config flow region selection."""

from unittest.mock import patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock.api import TTLockAuthImplementation
from custom_components.ttlock.const import (
    CONF_BLUETOOTH_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_SLOW_POLL_INTERVAL,
    CONF_WEBHOOK_URL,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component

TOKEN = {
    "access_token": "access",
    "refresh_token": "refresh",
    "expires_in": 7776000,
    "token_type": "Bearer",
    "scope": "",
}


@pytest.mark.parametrize(
    ("region", "token_url"),
    [
        ("eu", "https://euapi.ttlock.com/oauth2/token"),
        ("cn", "https://cnapi.ttlock.com/oauth2/token"),
    ],
)
async def test_flow_persists_region_and_targets_its_token_url(
    hass: HomeAssistant, region: str, token_url: str
):
    """Selecting a region persists it and points login at that region's cloud."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "mocked"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    if result["step_id"] == "pick_implementation":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"implementation": "mocked"}
        )
    assert result["step_id"] == "auth"
    captured: dict[str, str] = {}

    async def fake_token_request(self: TTLockAuthImplementation, data: dict) -> dict:
        captured["token_url"] = self.token_url
        return dict(TOKEN)

    with (
        patch.object(TTLockAuthImplementation, "_token_request", fake_token_request),
        patch("custom_components.ttlock.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_REGION: region,
                CONF_USERNAME: "user",
                CONF_PASSWORD: "pass",
            },
        )

        # New webhook step - shows the URL so the user can register it
        # in the TTLock console, then submits to actually create the entry.
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "webhook"
        placeholders = result.get("description_placeholders") or {}
        assert placeholders["webhook_url"]

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REGION] == region
    assert result["data"][CONF_WEBHOOK_ID]
    assert captured["token_url"] == token_url


async def test_options_flow_saves_polling_cadence(hass: HomeAssistant):
    """The options flow persists the fast/slow polling intervals."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 45,
            CONF_SLOW_POLL_INTERVAL: 12,
            CONF_BLUETOOTH_ENABLED: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # coerced to int (NumberSelector yields floats) so the reload guard's
    # snapshot comparison stays clean
    assert entry.options[CONF_POLL_INTERVAL] == 45
    assert isinstance(entry.options[CONF_POLL_INTERVAL], int)
    assert entry.options[CONF_SLOW_POLL_INTERVAL] == 12
    assert isinstance(entry.options[CONF_SLOW_POLL_INTERVAL], int)
    assert entry.options[CONF_BLUETOOTH_ENABLED] is True


async def test_options_flow_can_disable_bluetooth(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 30,
            CONF_SLOW_POLL_INTERVAL: 6,
            CONF_BLUETOOTH_ENABLED: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BLUETOOTH_ENABLED] is False


async def test_options_flow_shows_webhook_url(hass: HomeAssistant):
    """The webhook URL is shown in the options flow so the user can see it later."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_WEBHOOK_ID: "wh-123",
            CONF_WEBHOOK_URL: "https://example.com/api/webhook/wh-123",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    placeholders = result.get("description_placeholders") or {}
    assert placeholders["webhook_url"] == "https://example.com/api/webhook/wh-123"


async def test_config_flow_adopts_sibling_webhook_id(hass: HomeAssistant):
    """Second account under the same client_id reuses the existing webhook_id."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "mocked"
    )

    existing = MockConfigEntry(
        domain=DOMAIN,
        data={"auth_implementation": "mocked", CONF_WEBHOOK_ID: "shared-wh"},
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    if result["step_id"] == "pick_implementation":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"implementation": "mocked"}
        )

    async def fake_token_request(self: TTLockAuthImplementation, data: dict) -> dict:
        return dict(TOKEN)

    with (
        patch.object(TTLockAuthImplementation, "_token_request", fake_token_request),
        patch("custom_components.ttlock.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REGION: "eu", CONF_USERNAME: "user2", CONF_PASSWORD: "pass"},
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEBHOOK_ID] == "shared-wh"


async def test_options_flow_shows_empty_url_when_ha_has_no_base_url(
    hass: HomeAssistant,
):
    """A stored webhook_id with no CONF_WEBHOOK_URL still renders the form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_WEBHOOK_ID: "wh-only"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ttlock.config_flow.webhook.async_generate_url",
        side_effect=NoURLAvailableError,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)

    placeholders = result.get("description_placeholders") or {}
    assert placeholders["webhook_url"] == ""


async def test_config_flow_ignores_other_integrations_webhook_ids(
    hass: HomeAssistant,
):
    """A sibling under a different credential does not get its webhook_id adopted."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "mocked"
    )

    unrelated = MockConfigEntry(
        domain=DOMAIN,
        data={"auth_implementation": "other-cred", CONF_WEBHOOK_ID: "other-wh"},
    )
    unrelated.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    if result["step_id"] == "pick_implementation":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"implementation": "mocked"}
        )

    async def fake_token_request(self: TTLockAuthImplementation, data: dict) -> dict:
        return dict(TOKEN)

    with (
        patch.object(TTLockAuthImplementation, "_token_request", fake_token_request),
        patch("custom_components.ttlock.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REGION: "eu", CONF_USERNAME: "user", CONF_PASSWORD: "pass"},
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEBHOOK_ID] != "other-wh"
