"""Test ttlock diagnostics."""

from custom_components.ttlock.const import DOMAIN
from custom_components.ttlock.diagnostics import async_get_config_entry_diagnostics
from custom_components.ttlock.models import Gateway, LockSummary
from homeassistant.core import HomeAssistant


async def test_diagnostics_includes_connectable_and_health(
    hass: HomeAssistant, component_setup, mock_api_responses, monkeypatch
):
    """Diagnostics surface connectable and last_update_success for troubleshooting."""
    mock_api_responses("default")

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
