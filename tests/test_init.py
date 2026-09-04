"""Test ttlock setup process."""

from datetime import timedelta
from unittest.mock import patch

from custom_components.ttlock import async_remove_config_entry_device
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_POLL_INTERVAL,
    CONF_SLOW_POLL_INTERVAL,
    CONF_WEBHOOK_STATUS,
    DOMAIN,
    TT_CAPTURE,
    TT_LOCKS,
)
from custom_components.ttlock.models import Gateway, LockSummary
from custom_components.ttlock.webhook import WebhookHandler
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_WEBHOOK_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.network import NoURLAvailableError


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


async def test_no_setup_issue_for_entry_joining_confirmed_group(
    hass, mock_api_responses, multi_account_credential, new_mocked_entry
):
    """A sibling joining an already-confirmed group isn't told to re-register."""
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

    with patch(
        "homeassistant.helpers.issue_registry.async_create_issue"
    ) as mock_create:
        assert await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    mock_create.assert_not_called()
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
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "16:72:4C:CC:01:C4")}
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
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "16:72:4C:CC:01:C4")}
    )
    assert device is not None

    hass.data[DOMAIN].pop(entry.entry_id)

    assert await async_remove_config_entry_device(hass, entry, device) is True


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
