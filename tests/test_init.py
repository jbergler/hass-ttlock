"""Test ttlock setup process."""

from datetime import timedelta
from time import time
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientConnectorError, ClientResponseError, ConnectionTimeoutError
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.ttlock import (
    _async_release_wifi_proxy,
    _mac_bytes,
    async_remove_config_entry_device,
    coordinator as coord_mod,
)
from custom_components.ttlock.api import RequestFailed, TTLockApi
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_CLOUD_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_SLOW_POLL_INTERVAL,
    CONF_WEBHOOK_STATUS,
    CONF_WIFI_LOCAL_ENABLED,
    DOMAIN,
    SIGNAL_NEW_DATA,
    SVC_CREATE_PASSCODE,
    TT_CAPTURE,
    TT_GATEWAYS,
    TT_LOCKS,
    TT_WIFI_PROXY,
)
from custom_components.ttlock.coordinator import (
    LockUpdateCoordinator,
    NoLocalAdminTransport,
)
from custom_components.ttlock.models import Gateway, LockSummary, WebhookEvent
from custom_components.ttlock.store import LockStateStore
from custom_components.ttlock.webhook import WebhookHandler
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_WEBHOOK_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .const import (
    MOCK_LOCK_AES_KEY,
    MOCK_LOCK_MAC,
    MOCK_LOCK_VERSION,
    WEBHOOK_LOCK_10AM_UTC,
)

TOKEN_URL = "https://euapi.ttlock.com/oauth2/token"


async def test_setup_unload_and_reload_entry(hass, component_setup, mock_api_responses):
    """Test entry setup and unload."""
    mock_api_responses("default")
    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    assert isinstance(hass.data[DOMAIN][entry.entry_id][TT_CAPTURE], LockTrafficCapture)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state == ConfigEntryState.NOT_LOADED


def _wifi_lock(lock_id: int = 7252408, mac: str = MOCK_LOCK_MAC) -> LockSummary:
    return LockSummary(
        lockId=lock_id,
        lockAlias="WiFi Lock",
        lockMac=mac,
        featureValue=f"{2**56:X}",
        userType="110301",
    )


async def _setup_wifi_entry(
    hass, new_mocked_entry, options: dict, locks: list[LockSummary]
) -> MockConfigEntry:
    entry = new_mocked_entry(options=options)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks",
        AsyncMock(return_value=locks),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_nothing_listens_for_wifi_locks_by_default(
    hass,
    multi_account_credential,
    new_mocked_entry,
    mock_api_responses,
    stub_wifi_proxy_bind,
):
    mock_api_responses("default")
    await multi_account_credential(hass)

    await _setup_wifi_entry(hass, new_mocked_entry, {}, [_wifi_lock()])

    assert TT_WIFI_PROXY not in hass.data[DOMAIN]
    stub_wifi_proxy_bind.assert_not_awaited()


@pytest.mark.parametrize(
    ("mac", "raw"),
    [
        ("0A:0B:0C:0D:0E:0F", bytes.fromhex("0a0b0c0d0e0f")),
        ("0a-0b-0c-0d-0e-0f", bytes.fromhex("0a0b0c0d0e0f")),
        ("0A:0B:0C:0D:0E:ZZ", None),  # not hex
        ("0A:0B:0C", None),  # too short
    ],
)
def test_a_lock_mac_is_read_as_six_raw_bytes(mac, raw):
    assert _mac_bytes(mac) == raw


async def test_wifi_proxy_lifecycle(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    """Opted in, only WiFi locks are registered; the last release stops it."""
    mock_api_responses("default")
    await multi_account_credential(hass)
    not_wifi = _cached_summary().model_copy(
        update={"id": 2, "mac": "00:00:00:00:00:02"}
    )

    entry = await _setup_wifi_entry(
        hass,
        new_mocked_entry,
        {CONF_WIFI_LOCAL_ENABLED: True},
        [_wifi_lock(), not_wifi],
    )

    proxy = hass.data[DOMAIN][TT_WIFI_PROXY]
    assert list(proxy._by_id) == [7252408]  # noqa: SLF001
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert TT_WIFI_PROXY not in hass.data[DOMAIN]


async def test_releasing_wifi_locks_once_the_proxy_is_gone_does_nothing(hass):
    await _async_release_wifi_proxy(hass, [(7252408, object())])

    assert TT_WIFI_PROXY not in hass.data.get(DOMAIN, {})


async def test_a_sibling_unload_keeps_the_other_entrys_wifi_locks(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    options = {CONF_WIFI_LOCAL_ENABLED: True}
    entry_a = await _setup_wifi_entry(hass, new_mocked_entry, options, [_wifi_lock()])
    await _setup_wifi_entry(
        hass, new_mocked_entry, options, [_wifi_lock(2, "00:00:00:00:00:02")]
    )

    assert await hass.config_entries.async_unload(entry_a.entry_id)

    proxy = hass.data[DOMAIN][TT_WIFI_PROXY]
    assert list(proxy._by_id) == [2]  # noqa: SLF001


async def test_a_cloud_off_entry_takes_its_wifi_lock_over(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    mock_api_responses("default")
    await multi_account_credential(hass)

    await _setup_wifi_entry(
        hass,
        new_mocked_entry,
        {CONF_WIFI_LOCAL_ENABLED: True, CONF_CLOUD_ENABLED: False},
        [_wifi_lock()],
    )

    registration = hass.data[DOMAIN][TT_WIFI_PROXY]._by_id[7252408]  # noqa: SLF001
    assert registration.cloud_enabled() is False


async def test_a_bind_failure_raises_a_repair(
    hass,
    multi_account_credential,
    new_mocked_entry,
    mock_api_responses,
    stub_wifi_proxy_bind,
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    stub_wifi_proxy_bind.return_value = False

    entry = await _setup_wifi_entry(
        hass, new_mocked_entry, {CONF_WIFI_LOCAL_ENABLED: True}, [_wifi_lock()]
    )

    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wifi_proxy_bind_failed")

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, "wifi_proxy_bind_failed")


async def test_reload_releases_the_webhook_listener(
    hass, component_setup, mock_api_responses
):
    """The coordinator a reload replaced must stop handling webhook events.

    It is unreferenced but the dispatcher still holds it, so a leak is only
    visible through the stale object: it keeps updating its own state.
    """
    mock_api_responses("default")
    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    stale = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert stale.data.locked is False

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0] is not stale

    async_dispatcher_send(
        hass, SIGNAL_NEW_DATA, WebhookEvent.model_validate(WEBHOOK_LOCK_10AM_UTC)
    )
    await hass.async_block_till_done()

    assert stale.data.locked is False


async def test_changing_options_reloads_with_new_interval(
    hass, component_setup, mock_api_responses
):
    """Updating the polling options reloads the entry so coordinators pick up
    the new cadence (see _reload_on_options_update)."""
    mock_api_responses("default")
    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert coordinator.update_interval == timedelta(minutes=30)

    hass.config_entries.async_update_entry(
        entry, options={CONF_POLL_INTERVAL: 90, CONF_SLOW_POLL_INTERVAL: 24}
    )
    await hass.async_block_till_done()

    # reload rebuilt the coordinators with the new cadence
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert coordinator.update_interval == timedelta(minutes=90)


async def test_disabling_bluetooth_tears_down_tracking(
    hass, component_setup, mock_api_responses, mock_bluetooth
):
    """Turning the option off reloads the entry, which releases the radio."""
    mock_api_responses("default")
    await component_setup()
    assert mock_bluetooth.advertisement is not None

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(
        entry, options={CONF_BLUETOOTH_ENABLED: False}
    )
    await hass.async_block_till_done()

    assert mock_bluetooth.unsubscribed == ["advertisement", "unavailable"]
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert coordinator.ble_enabled is False


async def test_capture_is_shared_across_config_entries(
    hass, mock_api_responses, multi_account_credential, new_mocked_entry
):
    """A single LockTrafficCapture is shared, even with two TTLock accounts."""
    mock_api_responses("default")
    await multi_account_credential(hass)

    # entry_b must not be added until entry_a's setup has finished: adding
    # both before any setup call would let HA's component-level bootstrap
    # (triggered by entry_a's setup, the domain's first) auto-forward-setup
    # entry_b too, racing the explicit call below.
    entry_a = new_mocked_entry()
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)

    entry_b = new_mocked_entry()
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_b.entry_id)

    await hass.async_block_till_done(wait_background_tasks=True)

    capture_a = hass.data[DOMAIN][entry_a.entry_id][TT_CAPTURE]
    capture_b = hass.data[DOMAIN][entry_b.entry_id][TT_CAPTURE]
    assert capture_a is capture_b

    assert await hass.config_entries.async_unload(entry_a.entry_id)
    assert hass.data[DOMAIN][entry_b.entry_id][TT_CAPTURE] is capture_b

    assert await hass.config_entries.async_unload(entry_b.entry_id)

    # re-adding a fresh entry after the last one unloads must not reuse the
    # torn-down capture instance
    entry_c = new_mocked_entry()
    entry_c.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_c.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.data[DOMAIN][entry_c.entry_id][TT_CAPTURE] is not capture_b


@patch(
    "homeassistant.components.webhook.async_generate_url",
    side_effect=NoURLAvailableError,
)
async def test_no_url(hass, component_setup, mock_api_responses):
    mock_api_responses("default")
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock:
        assert await component_setup()
        assert mock.assert_called


async def test_webhook_failure_does_not_fail_running_entry_setup(
    hass, component_setup, mock_api_responses, mock_cloud
):
    """Webhook infrastructure is best effort after lock discovery succeeds."""
    mock_api_responses("default")
    mock_cloud.async_active_subscription.return_value = True
    mock_cloud.async_get_or_create_cloudhook.side_effect = ValueError(
        "cloudhook creation failed"
    )

    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock_issue:
        assert await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is ConfigEntryState.LOADED
    assert len(hass.data[DOMAIN][entry.entry_id][TT_LOCKS]) == 1
    assert any(
        call.args[2] == "webhook_registration_failed_client-id"
        for call in mock_issue.call_args_list
    )


async def test_group_refcount_unwinds_after_no_url_failure(
    hass, component_setup, mock_api_responses
):
    """An entry that fails to register (NoURLAvailableError) must not leave
    its group's refcount permanently inflated - otherwise the group would
    never reach a refcount of 0 and its webhook would never be unregistered,
    even once every entry using it is gone.
    """
    mock_api_responses("default")
    with patch(
        "homeassistant.components.webhook.async_generate_url",
        side_effect=NoURLAvailableError,
    ):
        await component_setup()

    with patch(
        "custom_components.ttlock.webhook.webhook_unregister"
    ) as mock_unregister:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    mock_unregister.assert_called_once()


async def test_setup_with_non_connectable_lock(
    hass, component_setup, mock_api_responses, monkeypatch, caplog
):
    """Non-connectable locks still get entities, marked unavailable with a reason."""
    mock_api_responses("default")

    async def mock_get_locks(*args, **kwargs):
        return [
            LockSummary(
                lockId=7252408,
                lockAlias="Connectable Lock",
                lockMac="00:00:00:00:00:01",
                hasGateway=1,
                userType="110301",
            ),
            LockSummary(
                lockId=2,
                lockAlias="No Gateway Lock",
                lockMac="00:00:00:00:00:02",
                hasGateway=0,
                userType="110301",
            ),
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
    )

    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    coordinators = {
        coordinator.lock_id: coordinator
        for coordinator in hass.data[DOMAIN][entry.entry_id][TT_LOCKS]
    }
    assert coordinators[7252408].connectable is True
    assert coordinators[2].connectable is False

    connectable_entity = next(iter(coordinators[7252408].entities))
    unavailable_entity = next(iter(coordinators[2].entities))

    assert connectable_entity.available is True
    assert unavailable_entity.available is False
    assert "unavailable_reason" in unavailable_entity.extra_state_attributes

    assert "1 lock(s) have no gateway or WiFi" in caplog.text


async def test_setup_with_a_gatewayless_lock_in_bluetooth_range(
    hass,
    component_setup,
    mock_api_responses,
    mock_bluetooth,
    ble_advertisement,
    monkeypatch,
    caplog,
):
    """A lock only the radio can reach is polled and available from the start."""
    mock_api_responses("default")

    async def mock_get_locks(*args, **kwargs):
        return [
            LockSummary(
                lockId=2,
                lockAlias="No Gateway Lock",
                lockMac="00:00:00:00:00:02",
                hasGateway=0,
                userType="110301",
            ),
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
    )
    mock_bluetooth.seed = ble_advertisement(address="00:00:00:00:00:02")

    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert coordinator.cloud_connectable is False
    assert coordinator.connectable is True
    assert coordinator.update_interval is not None

    entity = next(iter(coordinator.entities))
    assert entity.available is True
    assert "have no gateway or WiFi" not in caplog.text


async def test_webhook_registered_and_unregistered_once_across_shared_entries(
    hass, mock_api_responses, multi_account_credential, new_mocked_entry
):
    """Two TTLock accounts under one developer application share one webhook."""
    mock_api_responses("default")
    await multi_account_credential(hass)

    entry_a = new_mocked_entry()
    entry_a.add_to_hass(hass)

    with patch("custom_components.ttlock.webhook.webhook_register") as mock_register:
        assert await hass.config_entries.async_setup(entry_a.entry_id)

        # entry_b must not be added until entry_a's setup has finished, same
        # reasoning as test_capture_is_shared_across_config_entries above.
        entry_b = new_mocked_entry()
        entry_b.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry_b.entry_id)

        await hass.async_block_till_done(wait_background_tasks=True)

    mock_register.assert_called_once()
    assert entry_a.data[CONF_WEBHOOK_ID] == entry_b.data[CONF_WEBHOOK_ID]

    with patch(
        "custom_components.ttlock.webhook.webhook_unregister"
    ) as mock_unregister:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    mock_unregister.assert_called_once()


async def test_setup_issue_raised_once_for_shared_entries(
    hass, mock_api_responses, multi_account_credential, new_mocked_entry
):
    """A group sharing a webhook raises one setup repair issue, not one per entry."""
    mock_api_responses("default")
    await multi_account_credential(hass)

    entry_a = new_mocked_entry()
    entry_a.add_to_hass(hass)

    with patch(
        "homeassistant.helpers.issue_registry.async_create_issue"
    ) as mock_create:
        assert await hass.config_entries.async_setup(entry_a.entry_id)

        entry_b = new_mocked_entry()
        entry_b.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry_b.entry_id)

        await hass.async_block_till_done(wait_background_tasks=True)

    mock_create.assert_called_once()
    assert mock_create.call_args.args[2] == "webhook_setup_client-id"


async def test_entry_joining_confirmed_group_inherits_webhook_status(
    hass, mock_api_responses, multi_account_credential, new_mocked_entry
):
    """A sibling joining an already-confirmed group picks up its confirmed flag."""
    mock_api_responses("default")
    await multi_account_credential(hass)

    entry_a = new_mocked_entry()
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    handler = WebhookHandler(hass, entry_a, client_id="client-id")
    handler.async_resolve_setup_issue()
    assert entry_a.data[CONF_WEBHOOK_STATUS] is True

    entry_b = new_mocked_entry()
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry_b.data[CONF_WEBHOOK_STATUS] is True


async def test_removing_last_entry_deletes_cloudhook(
    hass, component_setup, mock_api_responses, mock_cloud
):
    """Removing the sole entry using a webhook_id deletes its cloudhook."""
    mock_api_responses("default")
    mock_cloud.async_active_subscription.return_value = True
    mock_cloud.async_get_or_create_cloudhook.return_value = (
        "https://hooks.nabucasa.com/abc"
    )

    await component_setup()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    assert await hass.config_entries.async_remove(entry.entry_id)

    mock_cloud.async_delete_cloudhook.assert_awaited_once_with(hass, webhook_id)


async def test_removing_one_of_several_shared_entries_keeps_cloudhook(
    hass, mock_api_responses, mock_cloud, multi_account_credential, new_mocked_entry
):
    """Removing one of several entries sharing a webhook_id must not delete it."""
    mock_api_responses("default")
    mock_cloud.async_active_subscription.return_value = True
    mock_cloud.async_get_or_create_cloudhook.return_value = (
        "https://hooks.nabucasa.com/abc"
    )
    await multi_account_credential(hass)

    entry_a = new_mocked_entry()
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)

    entry_b = new_mocked_entry()
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_b.entry_id)

    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry_a.data[CONF_WEBHOOK_ID] == entry_b.data[CONF_WEBHOOK_ID]

    assert await hass.config_entries.async_remove(entry_a.entry_id)

    mock_cloud.async_delete_cloudhook.assert_not_called()

    assert await hass.config_entries.async_remove(entry_b.entry_id)

    mock_cloud.async_delete_cloudhook.assert_awaited_once()


async def test_removing_entry_without_cloudhook_is_a_noop(
    hass, component_setup, mock_api_responses, mock_cloud
):
    """Removal is a no-op, not an error, for a non-cloud user."""
    mock_api_responses("default")

    await component_setup()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    assert await hass.config_entries.async_remove(entry.entry_id)

    mock_cloud.async_delete_cloudhook.assert_not_called()


async def test_remove_config_entry_device_blocks_active_lock(
    hass, component_setup, mock_api_responses
):
    """A device for a lock still on the account can't be manually deleted."""
    mock_api_responses("default")
    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "16:72:4C:CC:01:C4"), entry.entry_id
    )
    assert device is not None

    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_remove_config_entry_device_allows_removed_lock(
    hass, component_setup, mock_api_responses
):
    """A device left behind after its lock was removed from the account can be deleted."""
    mock_api_responses("default")
    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    orphaned_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "00:00:00:00:00:99")},
    )

    assert await async_remove_config_entry_device(hass, entry, orphaned_device) is True


async def test_remove_config_entry_device_blocks_active_gateway(
    hass, component_setup, mock_api_responses, monkeypatch
):
    """A device for a gateway still on the account can't be manually deleted."""
    mock_api_responses("default")

    async def mock_get_gateways(*args, **kwargs):
        return [
            Gateway(
                gatewayId=1,
                gatewayName="Test Gateway",
                gatewayMac="11:22:33:44:55:66",
                isOnline=True,
            )
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_gateways", mock_get_gateways
    )

    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    gateway_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "11:22:33:44:55:66")},
    )

    assert await async_remove_config_entry_device(hass, entry, gateway_device) is False


async def test_remove_config_entry_device_allows_when_entry_data_missing(
    hass, component_setup, mock_api_responses
):
    """A failed/unloaded entry has nothing to check against, so removal is allowed."""
    mock_api_responses("default")
    await component_setup()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "16:72:4C:CC:01:C4"), entry.entry_id
    )
    assert device is not None

    entry_data = hass.data[DOMAIN].pop(entry.entry_id)

    assert await async_remove_config_entry_device(hass, entry, device) is True
    hass.data[DOMAIN][entry.entry_id] = entry_data


async def test_removing_entry_whose_cloudhook_was_never_created_is_a_noop(
    hass, component_setup, mock_api_responses, mock_cloud
):
    """Removal is a no-op, not an error, for a cloud user whose webhook_id was
    never actually converted to a cloudhook (eg. try_generate_cloudhook
    returned None despite an active subscription) - hass_nabucasa's
    Cloudhooks.async_delete raises a bare ValueError for this, not
    cloud.CloudNotAvailable.
    """
    mock_api_responses("default")
    mock_cloud.async_active_subscription.return_value = True
    mock_cloud.async_delete_cloudhook.side_effect = ValueError(
        "Hook is not enabled for the cloud."
    )

    await component_setup()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    assert await hass.config_entries.async_remove(entry.entry_id)


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up entry without asserting it loaded - component_setup assumes it did."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass,
        DOMAIN,
        ClientCredential("client-id", "client-secret"),
        entry.data["auth_implementation"],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize(
    "error",
    [
        ClientConnectorError(MagicMock(), OSError("Network is unreachable")),
        ConnectionTimeoutError(
            "Connection timeout to host https://euapi.ttlock.com/v3/lock/list"
            "?clientId=client-id&accessToken=mock-access-token"
        ),
        TimeoutError(),
        ClientResponseError(MagicMock(), (), status=503),
        RequestFailed("API returned: internal server error", errcode=90000),
    ],
    ids=["connector", "connection_timeout", "timeout", "http_503", "errcode_90000"],
)
async def test_transient_get_locks_failure_retries_setup(
    hass, config_entry, mock_api_responses, error
):
    """Cloud unreachable at startup (eg. HA booted before the internet came
    back after a power outage) must end in SETUP_RETRY so HA retries on its
    own, not in a permanent SETUP_ERROR.
    """
    mock_api_responses("default")
    with patch("custom_components.ttlock.api.TTLockApi.get_locks", side_effect=error):
        await _setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    # the reason is user-visible - aiohttp's own message would leak the
    # request URL, accessToken included
    assert config_entry.reason is not None
    assert "mock-access-token" not in config_entry.reason
    # no half-initialized runtime data left behind while waiting to retry
    assert config_entry.entry_id not in hass.data[DOMAIN]


async def test_setup_recovers_once_cloud_is_reachable(
    hass, config_entry, mock_api_responses
):
    """A transient failure on the first attempt, then success on HA's retry."""
    mock_api_responses("default")
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks",
        side_effect=ConnectionTimeoutError(),
    ):
        await _setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    # connectivity is back: mock_api_responses' get_locks answers again
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.state is ConfigEntryState.LOADED
    assert len(hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS]) == 1


@pytest.mark.parametrize("errcode", [10003, 10004, 10011])
async def test_auth_errcode_from_get_locks_is_a_setup_error(
    hass, config_entry, mock_api_responses, errcode
):
    """Invalid/revoked credentials won't fix themselves on retry: without a
    cache to start from they are a setup error, handed to reauth.
    """
    mock_api_responses("default")
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks",
        side_effect=RequestFailed("API returned: token invalid", errcode=errcode),
    ):
        await _setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR


def _entry_with_expired_token() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": "mocked",
            "token": {
                "refresh_token": "mock-refresh-token",
                "access_token": "mock-access-token",
                "type": "Bearer",
                "expires_in": 60,
                "expires_at": time() - 1000,
                "scope": "",
            },
        },
    )


@pytest.mark.parametrize(
    "token_response",
    [
        {"exc": ClientConnectorError(MagicMock(), OSError("Network is unreachable"))},
        {"exc": TimeoutError()},
        {"status": 502},
    ],
    ids=["connector", "timeout", "http_502"],
)
async def test_transient_token_refresh_failure_retries_setup(
    hass, aioclient_mock: AiohttpClientMocker, token_response
):
    """get_locks is the first call to need the token, so an expired one is
    refreshed right there - that refresh hitting a dead network must retry too.
    """
    aioclient_mock.post(TOKEN_URL, **token_response)
    entry = _entry_with_expired_token()

    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert aioclient_mock.call_count == 1


async def test_rejected_token_refresh_is_a_setup_error(
    hass, aioclient_mock: AiohttpClientMocker
):
    """A refresh token the server rejects needs the user, not a retry."""
    aioclient_mock.post(TOKEN_URL, status=401, json={"error": "invalid_grant"})
    entry = _entry_with_expired_token()

    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR


# --- setup without the cloud: the cached entry ----------------------------------


def _cached_summary() -> LockSummary:
    return LockSummary(
        lockId=7252408,
        lockAlias="Front Door",
        lockMac=MOCK_LOCK_MAC,
        featureValue="F44354CD5F3",
        hasGateway=1,
        userType="110301",
    )


def _seed_cache(hass_storage, entry_id: str, **entry_extra) -> None:
    """A store as a previous run with the cloud reachable would have left it."""
    hass_storage["ttlock_lock_state"] = {
        "version": 1,
        "key": "ttlock_lock_state",
        "data": {
            "7252408": {
                "ble_version": MOCK_LOCK_VERSION,
                "ble_key": MOCK_LOCK_AES_KEY,
                "ble_unlock_key": "2345",
                "ble_lock_flag_pos": 3,
                "ble_admin_pwd": "4242",
                "detail": {"modelNum": "SN9206_PV53", "autoLockTime": 60},
            },
            "entries": {
                entry_id: {
                    "key_records": [
                        _cached_summary().model_dump(mode="json", by_alias=True)
                    ],
                    "uid": 777,
                    **entry_extra,
                }
            },
        },
    }


@pytest.mark.parametrize(
    "error",
    [
        ClientConnectorError(MagicMock(), OSError("Network is unreachable")),
        ClientResponseError(MagicMock(), (), status=503),
        ValueError("Expecting value: line 1 column 1"),
        RequestFailed("API returned: internal server error", errcode=90000),
    ],
    ids=["connector", "http_503", "bad_json", "errcode_90000"],
)
async def test_cloud_failure_with_a_cache_still_loads_the_entry(
    hass, hass_storage, config_entry, mock_api_responses, error
):
    """A key/list that fails for any cloud reason starts the entry from the cache."""
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    with patch("custom_components.ttlock.api.TTLockApi.get_locks", side_effect=error):
        await _setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS][0]
    assert coordinator.lock_id == 7252408
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"cloud_unreachable_{config_entry.entry_id}"
    )
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize(
    "error",
    [
        RequestFailed("API returned: token invalid", errcode=10004),
        ConfigEntryAuthFailed("refresh token rejected"),
    ],
    ids=["errcode_10004", "oauth_reauth"],
)
async def test_refused_credentials_with_a_cache_load_and_start_reauth(
    hass, hass_storage, config_entry, mock_api_responses, error
):
    """Refused credentials need the user, but the locks must keep working."""
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    with patch("custom_components.ttlock.api.TTLockApi.get_locks", side_effect=error):
        await _setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_refused_credentials_without_a_cache_start_reauth(
    hass, config_entry, mock_api_responses
):
    mock_api_responses("default")
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks",
        side_effect=RequestFailed("API returned: token invalid", errcode=10004),
    ):
        await _setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_cloud_down_entry_drives_the_lock_over_bluetooth(
    hass,
    hass_storage,
    config_entry,
    mock_bluetooth,
    ble_advertisement,
    monkeypatch,
):
    """The whole point: with the cloud gone the cached lock still unlocks."""
    unreachable = AsyncMock(
        side_effect=ClientConnectorError(MagicMock(), OSError("unreachable"))
    )
    monkeypatch.setattr(TTLockApi, "get", unreachable)
    monkeypatch.setattr(TTLockApi, "post", unreachable)
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(config_entry.data), options={CONF_BLE_CONTROL: True}
    )
    _seed_cache(hass_storage, entry.entry_id)
    mock_bluetooth.seed = ble_advertisement()
    control = AsyncMock()
    monkeypatch.setattr(coord_mod, "async_control", control)

    await _setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert coordinator.data.model == "SN9206_PV53"
    assert coordinator.data.auto_lock_seconds == 60

    unreachable.reset_mock()
    await hass.services.async_call(
        "lock",
        "unlock",
        {
            "entity_id": next(
                entity.entity_id
                for entity in coordinator.entities
                if entity.entity_id.startswith("lock.")
            )
        },
        blocking=True,
    )

    control.assert_awaited_once()
    assert control.await_args is not None
    assert control.await_args.args[6:8] == (777, 3)  # cached uid and lockFlagPos
    unreachable.assert_not_awaited()
    assert coordinator.data.locked is False


async def test_cloud_off_with_a_cache_never_enumerates(
    hass, hass_storage, multi_account_credential, new_mocked_entry, mock_api_responses
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    entry = new_mocked_entry(options={CONF_CLOUD_ENABLED: False})
    _seed_cache(hass_storage, entry.entry_id)
    entry.add_to_hass(hass)
    get_locks = AsyncMock()
    get_gateways = AsyncMock()

    with (
        patch("custom_components.ttlock.api.TTLockApi.get_locks", get_locks),
        patch("custom_components.ttlock.api.TTLockApi.get_gateways", get_gateways),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    get_locks.assert_not_awaited()
    get_gateways.assert_not_awaited()
    assert hass.data[DOMAIN][entry.entry_id][TT_GATEWAYS] is None


async def test_successful_enumeration_is_cached_with_the_uid(
    hass, hass_storage, config_entry, mock_api_responses
):
    mock_api_responses("default")
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "token": {**config_entry.data["token"], "uid": 777},
        },
    )
    await _setup_entry(hass, config_entry)

    cached = await hass.data[DOMAIN]["_lock_state_store"].async_get_entry(
        config_entry.entry_id
    )
    assert cached["uid"] == 777
    assert [record["lockId"] for record in cached["key_records"]] == [7252408]


async def test_empty_enumeration_keeps_the_cache(
    hass, hass_storage, config_entry, mock_api_responses
):
    """An empty key/list must not erase the only way to start offline."""
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    with patch("custom_components.ttlock.api.TTLockApi.get_locks", return_value=[]):
        await _setup_entry(hass, config_entry)

    assert len(hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS]) == 1
    cached = await hass.data[DOMAIN]["_lock_state_store"].async_get_entry(
        config_entry.entry_id
    )
    assert len(cached["key_records"]) == 1


async def test_slow_tier_enumeration_clears_the_repair_and_reloads_on_change(
    hass, hass_storage, config_entry, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks",
        side_effect=ClientConnectorError(MagicMock(), OSError("unreachable")),
    ):
        await _setup_entry(hass, config_entry)
    coordinator = hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS][0]
    hass.data[DOMAIN]["_ekey_cache"].clear()  # the slow tier is due again
    second = _cached_summary().model_copy(update={"id": 2, "mac": "00:00:00:00:00:02"})
    monkeypatch.setattr(
        TTLockApi, "get_locks", AsyncMock(return_value=[_cached_summary(), second])
    )

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await coordinator.async_refresh()

    assert not ir.async_get(hass).async_get_issue(
        DOMAIN, f"cloud_unreachable_{config_entry.entry_id}"
    )
    reload.assert_called_once_with(config_entry.entry_id)


async def test_gateway_failure_never_blocks_setup(
    hass, hass_storage, config_entry, mock_api_responses, monkeypatch
):
    """A failed gateway refresh leaves the cached gateways, unavailable."""
    mock_api_responses("default")
    gateway = Gateway(
        gatewayId=1, gatewayName="Hall", gatewayMac="11:22:33:44:55:66", isOnline=True
    )
    _seed_cache(
        hass_storage,
        config_entry.entry_id,
        gateways=[gateway.model_dump(mode="json", by_alias=True)],
    )
    monkeypatch.setattr(
        TTLockApi, "get_gateways", AsyncMock(side_effect=TimeoutError())
    )

    await _setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    gateways = hass.data[DOMAIN][config_entry.entry_id][TT_GATEWAYS]
    assert gateways.last_update_success is False
    assert gateways.data == {1: gateway}


async def test_removing_the_entry_purges_its_cached_secrets(
    hass, hass_storage, config_entry, mock_api_responses
):
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    await _setup_entry(hass, config_entry)

    assert await hass.config_entries.async_remove(config_entry.entry_id)

    store = LockStateStore(hass)
    assert await store.async_get(7252408) == {}
    assert await store.async_get_entry(config_entry.entry_id) == {}


async def test_removing_an_entry_running_on_its_cache_clears_its_repair(
    hass, hass_storage, config_entry, mock_api_responses
):
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks", side_effect=TimeoutError()
    ):
        await _setup_entry(hass, config_entry)
    issue_id = f"cloud_unreachable_{config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)

    assert await hass.config_entries.async_remove(config_entry.entry_id)

    assert not ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def test_setup_restores_cached_credentials_before_any_detail(
    hass, hass_storage, config_entry, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    _seed_cache(hass_storage, config_entry.entry_id)
    monkeypatch.setattr(
        TTLockApi, "get_lock", AsyncMock(side_effect=RuntimeError("cloud down"))
    )

    await _setup_entry(hass, config_entry)

    coordinator = hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS][0]
    credentials = coordinator.wifi_credentials()
    assert credentials is not None
    assert (credentials.admin_ps, credentials.lock_flag_pos) == (4242, 3)


async def test_cloud_off_create_passcode_never_reaches_the_cloud(
    hass, component_setup, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    coordinator = await component_setup()
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: False)
    )
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(TTLockApi, "add_passcode", cloud)

    with pytest.raises(NoLocalAdminTransport):
        await hass.services.async_call(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            {
                "entity_id": next(
                    entity.entity_id
                    for entity in coordinator.entities
                    if entity.entity_id.startswith("lock.")
                ),
                "passcode_name": "n",
                "passcode": "1234",
            },
            blocking=True,
        )
    cloud.assert_not_awaited()


async def test_a_partial_cached_detail_keeps_a_cloud_off_wifi_lock_registered(
    hass, hass_storage, new_mocked_entry, mock_api_responses
):
    """A detail cache holding only a local write must not wipe the lock's features.

    Before lock/detail was ever cached, a local auto-lock write stores just
    autoLockTime; restoring that on restart must leave the key record's
    features (WiFi), model and firmware alone.
    """
    mock_api_responses("default")
    entry = new_mocked_entry(
        options={CONF_WIFI_LOCAL_ENABLED: True, CONF_CLOUD_ENABLED: False}
    )
    _seed_cache(hass_storage, entry.entry_id)
    state = hass_storage["ttlock_lock_state"]["data"]
    state["7252408"]["detail"] = {"autoLockTime": 5}
    state["entries"][entry.entry_id]["key_records"] = [
        _wifi_lock().model_dump(mode="json", by_alias=True)
    ]
    await _setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]
    assert coordinator.data.auto_lock_seconds == 5
    assert list(hass.data[DOMAIN][TT_WIFI_PROXY]._by_id) == [7252408]  # noqa: SLF001
