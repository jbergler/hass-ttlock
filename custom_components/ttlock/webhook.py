"""The TTLock integration.

Receives TTLock's push notifications: this repo's webhook, registered with
your TTLock account (see https://open.ttlock.com/manager), is called
whenever a lock/passcode event happens. WebhookHandler.handle_webhook parses
the payload into WebhookEvent models and dispatches SIGNAL_NEW_DATA, which
LockUpdateCoordinator._process_webhook_data (coordinator.py) listens for.

TTLock's console accepts one callback URL per developer application
(client_id), not per TTLock account, but each config entry is one account
login - so config entries sharing a client_id must share one webhook
registration rather than each minting their own. See
docs/adr/0002-shared-webhook-per-developer-application.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets

from aiohttp.web import Request

from homeassistant.components import webhook
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_WEBHOOK_ID,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError

from .capture import LockTrafficCapture, log_and_capture
from .const import (
    CONF_WEBHOOK_STATUS,
    CONF_WEBHOOK_URL,
    DOMAIN,
    SIGNAL_NEW_DATA,
    get_device_logger,
)
from .models import WebhookEvent

_LOGGER = logging.getLogger(__name__)

# hass.data[DOMAIN][_GROUPS_KEY]: dict[client_id, _WebhookGroup]. Refcounts
# how many loaded config entries share one developer application's webhook
# registration, so it's registered on the first member's setup and only
# unregistered once the last member unloads - no single member is a
# privileged "owner".
_GROUPS_KEY = "_webhook_groups"

# Serializes the check-is-group-active-and-increment section of
# register_webhook (and its unregister_webhook counterpart) so two sibling
# entries loading concurrently at startup can't both decide they're first.
_GROUPS_LOCK = asyncio.Lock()


def _setup_issue_id(client_id: str) -> str:
    return f"webhook_setup_{client_id}"


class _WebhookGroup:
    """The shared webhook_id for one client_id, and how many entries use it."""

    def __init__(self, webhook_id: str, confirmed: bool = False) -> None:
        self.webhook_id = webhook_id
        self.refcount = 0
        # Only True once webhook_register has actually been called for this
        # webhook_id - distinct from refcount, since a member can join the
        # group (refcount+1) but bail out before registering (eg.
        # NoURLAvailableError), leaving the next member to still need to do
        # the real HA-side registration.
        self.registered = False
        # Has any member ever actually received webhook traffic? Tracked
        # here (not just in each entry's own CONF_WEBHOOK_STATUS) so a new
        # sibling joining an already-working group later in the same
        # runtime doesn't get told to go register a URL that's already live.
        self.confirmed = confirmed
        # Has the "register this URL" repair issue already been raised for
        # this group this runtime? All members share one issue_id, so raising
        # it again would just be a same-content no-op, but tracking this
        # avoids the redundant call.
        self.issue_raised = False


class WebhookHandler:
    """Responsible for setting up/processing webhook data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        capture: LockTrafficCapture | None = None,
        client_id: str = "",
    ) -> None:
        """Init the thing."""
        self.hass = hass
        self.entry = entry
        self.client_id = client_id
        self._capture = capture

    async def setup(self) -> None:
        """Actually register the webhook."""
        if self.hass.state == CoreState.running:
            await self.register_webhook()
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self.register_webhook
            )

    async def try_generate_cloudhook(self) -> str | None:
        """Create a cloudhook if possible."""
        # deferred: cloud pulls in optional heavy dependencies we don't want to
        # require at module import time, and this also lets tests mock it easily
        from homeassistant.components import cloud  # noqa: PLC0415

        if cloud.async_active_subscription(self.hass):
            try:
                return await cloud.async_create_cloudhook(
                    self.hass, self.entry.data[CONF_WEBHOOK_ID]
                )
            except cloud.CloudNotConnected:
                return None
        return None

    async def get_url(self) -> str:
        """Get the webhook url depending on the setup."""
        if CONF_WEBHOOK_URL in self.entry.data:
            return self.entry.data[CONF_WEBHOOK_URL]
        if cloudhook := await self.try_generate_cloudhook():
            return cloudhook
        return webhook.async_generate_url(self.hass, self.entry.data[CONF_WEBHOOK_ID])

    async def _group_members(self) -> list[ConfigEntry]:
        """Every config entry (loaded or not) authenticating via this client_id."""
        members = []
        for candidate in self.hass.config_entries.async_entries(DOMAIN):
            if candidate.entry_id == self.entry.entry_id:
                members.append(candidate)
                continue
            try:
                implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
                    self.hass, candidate
                )
            except (ValueError, KeyError):
                continue
            if implementation.client_id == self.client_id:  # ty: ignore[unresolved-attribute] - implementation is a TTLockAuthImplementation
                members.append(candidate)
        return members

    async def _resolve_group_webhook_id(self) -> tuple[str, bool]:
        """Pick the one webhook_id this client_id's group will share.

        Only called the first time this client_id's group becomes active in
        this runtime - reconciles config entries left over from before
        webhook sharing existed, which may each carry a different stale
        webhook_id. Prefers whichever webhook_id has actually received live
        traffic before (CONF_WEBHOOK_STATUS); if that's ambiguous (no
        confirmed value, or confirmed members disagree), falls back to entry
        order and raises a repair issue - with the guessed URL embedded
        directly in it, since the separate webhook-setup repair issue isn't a
        reliable place to point the user at: it gets resolved the moment any
        traffic arrives on the guessed webhook, while this one (which the
        user might not read until later) stays open regardless.

        Returns the chosen webhook_id and whether the group is confirmed
        (some member has already received live traffic on it).
        """
        members = await self._group_members()
        existing = {
            member.data[CONF_WEBHOOK_ID]
            for member in members
            if CONF_WEBHOOK_ID in member.data
        }
        confirmed_ids = {
            member.data[CONF_WEBHOOK_ID]
            for member in members
            if member.data.get(CONF_WEBHOOK_STATUS) and CONF_WEBHOOK_ID in member.data
        }

        ambiguous = False
        if not existing:
            canonical = secrets.token_hex()
        elif len(existing) == 1:
            canonical = next(iter(existing))
        elif len(confirmed_ids) == 1:
            canonical = next(iter(confirmed_ids))
        else:
            canonical = next(
                member.data[CONF_WEBHOOK_ID]
                for member in members
                if CONF_WEBHOOK_ID in member.data
            )
            ambiguous = True

        # Ambiguous means we can't actually trust the guess, even if it
        # happens to equal one of several conflicting previously-confirmed
        # ids - treating it as confirmed here would suppress the setup
        # notification carrying the URL below.
        confirmed = not ambiguous and canonical in confirmed_ids

        for member in members:
            data = dict(member.data)
            changed = False
            if data.get(CONF_WEBHOOK_ID) != canonical:
                data[CONF_WEBHOOK_ID] = canonical
                data.pop(CONF_WEBHOOK_URL, None)
                changed = True
            if confirmed and not data.get(CONF_WEBHOOK_STATUS):
                data[CONF_WEBHOOK_STATUS] = True
                changed = True
            if changed:
                self.hass.config_entries.async_update_entry(member, data=data)

        if ambiguous:
            try:
                webhook_url = webhook.async_generate_url(self.hass, canonical)
            except NoURLAvailableError:
                webhook_url = canonical
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"webhook_consolidation_ambiguous_{self.client_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="webhook_consolidation_ambiguous",
                translation_placeholders={"webhook_url": webhook_url},
                learn_more_url="https://open.ttlock.com/manager",
            )

        return canonical, confirmed

    def _sync_entry_to_group(self, group: _WebhookGroup) -> None:
        """Adopt the group's webhook_id and confirmed state onto this entry.

        Runs for every member, not just whoever resolved the group - a
        sibling joining an already-confirmed group later in the same
        runtime still needs its own CONF_WEBHOOK_STATUS set, or it'll raise a
        "register this URL" repair issue for a webhook that's already live.
        """
        data = dict(self.entry.data)
        changed = False
        if data.get(CONF_WEBHOOK_ID) != group.webhook_id:
            data[CONF_WEBHOOK_ID] = group.webhook_id
            data.pop(CONF_WEBHOOK_URL, None)
            changed = True
        if group.confirmed and not data.get(CONF_WEBHOOK_STATUS):
            data[CONF_WEBHOOK_STATUS] = True
            changed = True
        if changed:
            self.hass.config_entries.async_update_entry(self.entry, data=data)

    async def register_webhook(self, event: Event | None = None) -> None:
        """Set up a webhook to receive pushed data.

        Shared with any config entry using the same client_id.
        """
        async with _GROUPS_LOCK:
            groups: dict[str, _WebhookGroup] = self.hass.data.setdefault(
                DOMAIN, {}
            ).setdefault(_GROUPS_KEY, {})
            group = groups.get(self.client_id)
            if group is None:
                webhook_id, confirmed = await self._resolve_group_webhook_id()
                group = _WebhookGroup(webhook_id, confirmed)
                groups[self.client_id] = group
            group.refcount += 1

        self._sync_entry_to_group(group)

        # Registered as soon as this entry has joined the group (incrementing
        # refcount above), not gated on the registration below succeeding -
        # otherwise a NoURLAvailableError return below would leave this
        # entry's +1 with no way to ever decrement it back out, so the group
        # would never reach a refcount of 0 and its webhook would never be
        # unregistered. unregister_webhook/webhook_unregister are no-ops if
        # nothing was ever actually registered with HA.
        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self.unregister_webhook
        )

        try:
            webhook_url = await self.get_url()
            data = {**self.entry.data, CONF_WEBHOOK_URL: webhook_url}
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        except NoURLAvailableError:
            _LOGGER.exception("Could not find base URL for installation")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "no_webhook_url",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="no_webhook_url",
            )
            return
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "no_webhook_url")

        if CONF_WEBHOOK_STATUS not in self.entry.data and not group.issue_raised:
            self.async_create_setup_issue(webhook_url)
            group.issue_raised = True

        async with _GROUPS_LOCK:
            if not group.registered:
                _LOGGER.info("Webhook registered at %s", webhook_url)

                # Ensure the webhook is not registered already
                webhook_unregister(self.hass, group.webhook_id)

                webhook_register(
                    self.hass,
                    DOMAIN,
                    "TTLock",
                    group.webhook_id,
                    self.handle_webhook,
                )
                group.registered = True

    async def handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Request
    ) -> None:
        """Handle webhook callback."""

        success = False
        try:
            # {'lockId': ['7252408'], 'notifyType': ['1'], 'records': ['[{"lockId":7252408,"electricQuantity":93,"serverDate":1680810180029,"recordTypeFromLock":17,"recordType":7,"success":1,"lockMac":"16:72:4C:CC:01:C4","keyboardPwd":"<digits>","lockDate":1680810186000,"username":"Jonas"}]'], 'admin': ['jonas@lemon.nz'], 'lockMac': ['16:72:4C:CC:01:C4']}
            if data := await request.post():
                lock_id = (
                    int(lock_id_raw)  # ty: ignore[invalid-argument-type] - lockId is always a form field string, never a file upload
                    if (lock_id_raw := data.get("lockId"))
                    else None
                )
                logger = get_device_logger(lock_id) if lock_id is not None else _LOGGER
                log_and_capture(
                    self._capture, logger, lock_id, "Got webhook data: %s", data
                )
                for raw_records in data.getall("records", []):
                    for record in json.loads(raw_records):  # ty: ignore[invalid-argument-type] - always a JSON string, never a file upload
                        async_dispatcher_send(
                            hass, SIGNAL_NEW_DATA, WebhookEvent.model_validate(record)
                        )
                        success = True
            else:
                _LOGGER.debug("handle_webhook, empty payload: %s", await request.text())
        except ValueError:
            _LOGGER.exception("Exception parsing webhook data")
            return

        if success and CONF_WEBHOOK_STATUS not in self.entry.data:
            self.async_resolve_setup_issue()

    async def unregister_webhook(self, event: Event | None = None) -> None:
        """Remove the webhook (before stop), once the last group member unloads."""
        webhook_id = self.entry.data[CONF_WEBHOOK_ID]
        should_unregister = True
        async with _GROUPS_LOCK:
            domain_data = self.hass.data.get(DOMAIN, {})
            groups: dict[str, _WebhookGroup] = domain_data.get(_GROUPS_KEY, {})
            if group := groups.get(self.client_id):
                group.refcount -= 1
                should_unregister = group.refcount <= 0
                if should_unregister:
                    groups.pop(self.client_id, None)
            if not groups:
                domain_data.pop(_GROUPS_KEY, None)
        if should_unregister:
            webhook_unregister(self.hass, webhook_id)

    def async_create_setup_issue(self, uri: str) -> None:
        """Raise a repair issue asking the user to register this webhook URL.

        A repair issue rather than a persistent notification so it doesn't
        disappear from Settings > Repairs on its own - it's only cleared by
        async_resolve_setup_issue, once real traffic confirms it's actually
        registered with TTLock.
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _setup_issue_id(self.client_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="webhook_setup",
            translation_placeholders={
                "webhook_url": uri,
                "docs_url": "https://github.com/jbergler/hass-ttlock?tab=readme-ov-file#creating-an-oauth-app",
            },
            learn_more_url="https://open.ttlock.com/manager",
        )

    def async_resolve_setup_issue(self) -> None:
        """Resolve the repair issue shared by the whole group.

        Also marks every entry sharing this webhook as confirmed, and the
        in-memory group too, so a sibling joining later this same runtime
        doesn't get a repair issue raised for an already-live webhook.
        """
        webhook_id = self.entry.data.get(CONF_WEBHOOK_ID)
        for member in self.hass.config_entries.async_entries(DOMAIN):
            if member.data.get(CONF_WEBHOOK_ID) != webhook_id:
                continue
            if CONF_WEBHOOK_STATUS in member.data:
                continue
            self.hass.config_entries.async_update_entry(
                member, data={**member.data, CONF_WEBHOOK_STATUS: True}
            )
        groups: dict[str, _WebhookGroup] = self.hass.data.get(DOMAIN, {}).get(
            _GROUPS_KEY, {}
        )
        if group := groups.get(self.client_id):
            group.confirmed = True
        ir.async_delete_issue(self.hass, DOMAIN, _setup_issue_id(self.client_id))
