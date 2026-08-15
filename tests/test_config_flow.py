"""Test the TTLock config flow region selection."""

from unittest.mock import patch

import pytest

from custom_components.ttlock.api import TTLockAuthImplementation
from custom_components.ttlock.const import CONF_REGION, DOMAIN
from homeassistant import config_entries
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REGION] == region
    assert captured["token_url"] == token_url
