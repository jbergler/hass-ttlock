"""Test the TTLock config flow region selection."""

from unittest.mock import patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock.api import TTLockAuthImplementation
from custom_components.ttlock.const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_CLOUD_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_SLOW_POLL_INTERVAL,
    CONF_TRANSPORT_ORDER,
    CONF_WEBHOOK_URL,
    CONF_WIFI_LOCAL_ENABLED,
    DOMAIN,
    transport_order,
)
from custom_components.ttlock.models import LockSummary
from custom_components.ttlock.store import LockStateStore
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


async def test_options_flow_saves_transport_order_and_cloud_toggle(hass: HomeAssistant):
    """Transport order is stored as an ordered list; cloud can be turned off."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 30,
            CONF_SLOW_POLL_INTERVAL: 6,
            CONF_BLUETOOTH_ENABLED: True,
            CONF_WIFI_LOCAL_ENABLED: True,
            CONF_TRANSPORT_ORDER: "wifi_local-cloud-ble",
            CONF_CLOUD_ENABLED: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The dropdown string is persisted as the ordered list the coordinator reads.
    assert entry.options[CONF_TRANSPORT_ORDER] == ["wifi_local", "cloud", "ble"]
    assert entry.options[CONF_CLOUD_ENABLED] is False


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (None, ["ble", "wifi_local", "cloud"]),
        (["ble", "cloud"], ["ble", "wifi_local", "cloud"]),
        (["cloud", "ble"], ["cloud", "ble", "wifi_local"]),
        (["cloud"], ["ble", "wifi_local", "cloud"]),
        (["wifi_local", "cloud", "ble"], ["wifi_local", "cloud", "ble"]),
        (["ble", "gone", "ble", "cloud"], ["ble", "wifi_local", "cloud"]),
    ],
)
def test_a_saved_transport_order_gets_its_missing_transports_back(saved, expected):
    options = {} if saved is None else {CONF_TRANSPORT_ORDER: saved}
    assert transport_order(options) == expected


async def test_options_flow_offers_a_completed_legacy_transport_order(
    hass: HomeAssistant,
):
    """An order saved before WiFi-local existed is still a valid choice."""
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_TRANSPORT_ORDER: ["ble", "cloud"]}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TRANSPORT_ORDER] == ["ble", "wifi_local", "cloud"]


async def test_options_flow_wifi_local_is_off_by_default(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_WIFI_LOCAL_ENABLED] is False


@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        (
            {CONF_BLUETOOTH_ENABLED: False, CONF_BLE_CONTROL: True},
            {CONF_BLE_CONTROL: "ble_control_needs_bluetooth"},
        ),
        (
            {CONF_CLOUD_ENABLED: False},
            {CONF_CLOUD_ENABLED: "cloud_off_needs_local"},
        ),
    ],
    ids=["ble_control_without_bluetooth", "cloud_off_without_local"],
)
async def test_options_flow_refuses_unreachable_locks(
    hass: HomeAssistant, user_input: dict, errors: dict
):
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_POLL_INTERVAL: 45})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert entry.options == {CONF_POLL_INTERVAL: 45}


async def test_options_flow_refuses_cloud_off_for_member_only_locks(
    hass: HomeAssistant,
):
    """A MEMBER key has no local credentials: without the cloud it is dead."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    member = LockSummary(lockId=1, lockMac="AA:BB:CC:DD:EE:FF", userType="110302")
    await LockStateStore(hass).async_update_entry(
        entry.entry_id,
        key_records=[member.model_dump(mode="json", by_alias=True)],
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CLOUD_ENABLED: False, CONF_WIFI_LOCAL_ENABLED: True}
    )

    assert result["errors"] == {CONF_CLOUD_ENABLED: "cloud_off_needs_admin"}


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


async def _reauth(
    hass: HomeAssistant, token_uid: int, stored_uid: int | None = 7
) -> tuple[MockConfigEntry, dict]:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "mocked"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": "mocked",
            CONF_REGION: "cn",
            CONF_WEBHOOK_ID: "hook",
            "token": {**TOKEN, "access_token": "old"}
            | ({} if stored_uid is None else {"uid": stored_uid}),
        },
    )
    entry.add_to_hass(hass)
    captured: dict[str, str] = {}

    async def fake_token_request(self: TTLockAuthImplementation, data: dict) -> dict:
        captured["token_url"] = self.token_url
        return {**TOKEN, "access_token": "new", "uid": token_uid}

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "auth"
    with (
        patch.object(TTLockAuthImplementation, "_token_request", fake_token_request),
        patch("custom_components.ttlock.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REGION: "cn", CONF_USERNAME: "user", CONF_PASSWORD: "pass"},
        )
    return entry, {**result, **captured}


async def test_reauth_replaces_only_the_token(hass: HomeAssistant):
    entry, result = await _reauth(hass, token_uid=7)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert result["token_url"] == "https://cnapi.ttlock.com/oauth2/token"
    assert entry.data["token"]["access_token"] == "new"
    assert entry.data[CONF_WEBHOOK_ID] == "hook"
    assert entry.data[CONF_REGION] == "cn"


async def test_reauth_refuses_a_different_account(hass: HomeAssistant):
    entry, result = await _reauth(hass, token_uid=8)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["token"]["access_token"] == "old"


async def test_reauth_of_a_token_without_a_uid_is_accepted(hass: HomeAssistant):
    entry, result = await _reauth(hass, token_uid=8, stored_uid=None)

    assert result["reason"] == "reauth_successful"
    assert entry.data["token"]["uid"] == 8
