"""Tests for severing the inbound webhook when the cloud is disabled.

With the cloud off the TTLock cloud must not be able to call HA either,
so async_setup_entry registers the webhook only when cloud_enabled is True, and
otherwise unregisters the shared webhook (keeping any cloudhook, so its URL
survives) while respecting sibling entries that still have the cloud on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.ttlock.const import CONF_CLOUD_ENABLED, DOMAIN
from custom_components.ttlock.webhook import _GROUPS_KEY, WebhookHandler
from homeassistant.const import CONF_WEBHOOK_ID


async def test_setup_registers_webhook_when_cloud_enabled(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("default")
    with (
        patch.object(WebhookHandler, "setup", new=AsyncMock()) as setup,
        patch.object(WebhookHandler, "async_disable", new=AsyncMock()) as disable,
    ):
        await component_setup()

    setup.assert_awaited_once()
    disable.assert_not_called()


async def test_setup_disables_webhook_when_cloud_off(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    entry = new_mocked_entry(options={CONF_CLOUD_ENABLED: False})
    entry.add_to_hass(hass)

    with (
        patch.object(WebhookHandler, "setup", new=AsyncMock()) as setup,
        patch.object(WebhookHandler, "async_disable", new=AsyncMock()) as disable,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    disable.assert_awaited_once()
    setup.assert_not_called()


async def test_setup_survives_async_disable_failure(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    entry = new_mocked_entry(options={CONF_CLOUD_ENABLED: False})
    entry.add_to_hass(hass)

    with patch.object(
        WebhookHandler,
        "async_disable",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # Severing inbound is best-effort - a failure must not fail entry setup.
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]


async def test_reload_registers_then_severs_then_restores_webhook(
    hass, component_setup, config_entry, mock_api_responses
):
    mock_api_responses("default")
    await component_setup()

    # Cloud on by default: the shared webhook group is live.
    assert "client-id" in hass.data[DOMAIN].get(_GROUPS_KEY, {})

    # Flip the cloud off -> entry reloads -> the webhook group is torn down.
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CLOUD_ENABLED: False}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "client-id" not in hass.data[DOMAIN].get(_GROUPS_KEY, {})

    # Flip it back on -> entry reloads -> the webhook group returns.
    hass.config_entries.async_update_entry(config_entry, options={})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "client-id" in hass.data[DOMAIN].get(_GROUPS_KEY, {})


async def test_async_disable_unregisters_when_no_cloud_sibling(
    hass, multi_account_credential, new_mocked_entry
):
    await multi_account_credential(hass)
    entry = new_mocked_entry(options={CONF_CLOUD_ENABLED: False})
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_WEBHOOK_ID: "wh-1"}
    )

    handler = WebhookHandler(hass, entry, None, "client-id")
    with patch("custom_components.ttlock.webhook.webhook_unregister") as unregister:
        await handler.async_disable()

    unregister.assert_called_once_with(hass, "wh-1")


async def test_async_disable_keeps_webhook_for_cloud_enabled_sibling(
    hass, multi_account_credential, new_mocked_entry
):
    await multi_account_credential(hass)
    off = new_mocked_entry(options={CONF_CLOUD_ENABLED: False})
    off.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        off, data={**off.data, CONF_WEBHOOK_ID: "wh-1"}
    )
    on = new_mocked_entry(options={CONF_CLOUD_ENABLED: True})
    on.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        on, data={**on.data, CONF_WEBHOOK_ID: "wh-1"}
    )

    handler = WebhookHandler(hass, off, None, "client-id")
    with patch("custom_components.ttlock.webhook.webhook_unregister") as unregister:
        await handler.async_disable()

    # A sibling with the cloud still on needs the shared webhook - don't tear it down.
    unregister.assert_not_called()
