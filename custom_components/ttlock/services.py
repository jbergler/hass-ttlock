"""Services for ttlock integration."""

from datetime import time
import logging

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, WEEKDAYS
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.util.dt import as_utc

from .const import (
    CONF_ALL_DAY,
    CONF_AUTO_UNLOCK,
    CONF_END_TIME,
    CONF_SECONDS,
    CONF_START_TIME,
    CONF_WEEK_DAYS,
    DOMAIN,
    SVC_CLEANUP_PASSCODES,
    SVC_CONFIG_AUTOLOCK,
    SVC_CONFIG_PASSAGE_MODE,
    SVC_CREATE_PASSCODE,
    SVC_DELETE_PASSCODE,
    SVC_LIST_PASSCODES,
    SVC_LIST_RECORDS,
    SVC_MODIFY_PASSCODE,
    SVC_UPDATE_STATE,
)
from .coordinator import LockUpdateCoordinator, coordinator_for
from .models import AddPasscodeConfig, OnOff, PassageModeConfig

_LOGGER = logging.getLogger(__name__)

_LIST_RECORDS_SCHEMA = vol.Schema(
    {
        vol.Optional("start_date"): cv.datetime,
        vol.Optional("end_date"): cv.datetime,
        vol.Optional("page_size", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=200)
        ),
        vol.Optional("page_no", default=1): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)


class Services:
    """Wraps service handlers."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the service singleton."""
        self.hass = hass

    def register(self) -> None:
        """Register services for ttlock integration."""

        self.hass.services.register(
            DOMAIN,
            SVC_CONFIG_PASSAGE_MODE,
            self.handle_configure_passage_mode,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required(CONF_ENABLED): cv.boolean,
                    vol.Optional(CONF_AUTO_UNLOCK, default=False): cv.boolean,
                    vol.Optional(CONF_ALL_DAY, default=False): cv.boolean,
                    vol.Optional(CONF_START_TIME, default=time()): cv.time,
                    vol.Optional(CONF_END_TIME, default=time()): cv.time,
                    vol.Optional(CONF_WEEK_DAYS, default=WEEKDAYS): cv.weekdays,
                }
            ),
        )

        def _validate_start_and_end_time_together(config):
            """Ensure start_time and end_time are both present or both absent."""
            has_start = "start_time" in config
            has_end = "end_time" in config

            if has_start != has_end:
                raise vol.Invalid("start_time and end_time must be provided together")

            return config

        self.hass.services.register(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            self.handle_create_passcode,
            schema=vol.All(
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Required("passcode_name"): cv.string,
                        vol.Required("passcode"): cv.string,
                        vol.Optional("start_time"): cv.datetime,
                        vol.Optional("end_time"): cv.datetime,
                    }
                ),
                _validate_start_and_end_time_together,
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_MODIFY_PASSCODE,
            self.handle_modify_passcode,
            schema=vol.All(
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Required("passcode_id"): cv.positive_int,
                        vol.Required("passcode_name"): cv.string,
                        vol.Required("passcode"): cv.string,
                        vol.Optional("start_time"): cv.datetime,
                        vol.Optional("end_time"): cv.datetime,
                    }
                ),
                _validate_start_and_end_time_together,
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_DELETE_PASSCODE,
            self.handle_delete_passcode,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("passcode_id"): cv.positive_int,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_CLEANUP_PASSCODES,
            self.handle_cleanup_passcodes,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_LIST_PASSCODES,
            self.handle_list_passcodes,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_LIST_RECORDS,
            self.handle_list_records,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Optional("start_date"): cv.datetime,
                    vol.Optional("end_date"): cv.datetime,
                    vol.Optional("page_size", default=50): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=200)
                    ),
                    vol.Optional("page_no", default=1): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_CONFIG_AUTOLOCK,
            self.handle_configure_autolock,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required(CONF_ENABLED): cv.boolean,
                    vol.Optional(CONF_SECONDS): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=60)
                    ),
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_UPDATE_STATE,
            self.handle_update_state,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                }
            ),
        )

    def _get_coordinators(self, call: ServiceCall) -> dict[str, LockUpdateCoordinator]:
        """Get coordinators for the requested entities.

        Returns a dictionary mapping entity_ids to their coordinators.
        Filters out any entity_ids that don't have associated coordinators.

        Args:
            call: The service call containing entity_ids

        Returns:
            A dictionary where keys are entity_ids and values are their corresponding
            LockUpdateCoordinators. Only includes entries where a coordinator exists.

        """
        entity_ids = call.data.get(ATTR_ENTITY_ID)
        if entity_ids:
            return {
                entity_id: coordinator
                for entity_id in entity_ids
                if (coordinator := coordinator_for(self.hass, entity_id))
            }
        return {}

    async def handle_list_passcodes(self, call: ServiceCall) -> ServiceResponse:
        """List all passcodes for the selected locks."""
        passcodes = {}

        for entity_id, coordinator in self._get_coordinators(call).items():
            codes = await coordinator.api.list_passcodes(coordinator.lock_id)
            passcodes[entity_id] = [
                {
                    "name": code.name,
                    "id": code.id,
                    "passcode": code.passcode,
                    "type": code.type.name if code.type is not None else None,
                    "start_date": code.start_date,
                    "end_date": code.end_date,
                    "expired": code.expired,
                }
                for code in codes
            ]

        return {"passcodes": passcodes}

    async def handle_configure_passage_mode(self, call: ServiceCall):
        """Enable passage mode for the given entities."""
        start_time = call.data.get(CONF_START_TIME)
        end_time = call.data.get(CONF_END_TIME)
        days = [WEEKDAYS.index(day) + 1 for day in call.data.get(CONF_WEEK_DAYS)]

        config = PassageModeConfig(
            passageMode=OnOff.on if call.data.get(CONF_ENABLED) else OnOff.off,
            autoUnlock=OnOff.on if call.data.get(CONF_AUTO_UNLOCK) else OnOff.off,
            isAllDay=OnOff.on if call.data.get(CONF_ALL_DAY) else OnOff.off,
            startDate=start_time.hour * 60 + start_time.minute,
            endDate=end_time.hour * 60 + end_time.minute,
            weekDays=days,
        )

        for _entity_id, coordinator in self._get_coordinators(call).items():
            if await coordinator.api.set_passage_mode(coordinator.lock_id, config):
                coordinator.data.passage_mode_config = config
                coordinator.async_update_listeners()

    async def handle_create_passcode(self, call: ServiceCall):
        """Create a new passcode for the given entities."""

        start_time_val = call.data.get("start_time")
        if start_time_val:
            start_time_utc = as_utc(start_time_val)
            start_time = int(start_time_utc.timestamp() * 1000)
        else:
            start_time = None

        end_time_val = call.data.get("end_time")
        if end_time_val:
            end_time_utc = as_utc(end_time_val)
            end_time = int(end_time_utc.timestamp() * 1000)
        else:
            end_time = None

        config = AddPasscodeConfig(
            passcode=call.data.get("passcode"),
            passcodeName=call.data.get("passcode_name"),
            startDate=start_time,
            endDate=end_time,
        )

        for _entity_id, coordinator in self._get_coordinators(call).items():
            await coordinator.api.add_passcode(coordinator.lock_id, config)

    async def handle_modify_passcode(self, call: ServiceCall):
        """Modify an existing passcode for the given entities."""

        start_time_val = call.data.get("start_time")
        if start_time_val:
            start_time_utc = as_utc(start_time_val)
            start_time = int(start_time_utc.timestamp() * 1000)
        else:
            start_time = None

        end_time_val = call.data.get("end_time")
        if end_time_val:
            end_time_utc = as_utc(end_time_val)
            end_time = int(end_time_utc.timestamp() * 1000)
        else:
            end_time = None

        config = AddPasscodeConfig(
            passcode=call.data.get("passcode"),
            passcodeName=call.data.get("passcode_name"),
            startDate=start_time,
            endDate=end_time,
        )

        passcode_id = call.data.get("passcode_id")

        for _entity_id, coordinator in self._get_coordinators(call).items():
            await coordinator.api.modify_passcode(
                coordinator.lock_id, passcode_id, config
            )

    async def handle_delete_passcode(self, call: ServiceCall):
        """Delete a specific passcode from the given entities."""

        passcode_id = call.data.get("passcode_id")

        for _entity_id, coordinator in self._get_coordinators(call).items():
            await coordinator.api.delete_passcode(coordinator.lock_id, passcode_id)

    async def handle_cleanup_passcodes(self, call: ServiceCall) -> ServiceResponse:
        """Clean up expired passcodes for the given entities."""
        removed = {}

        for entity_id, coordinator in self._get_coordinators(call).items():
            removed_for_lock = []
            codes = await coordinator.api.list_passcodes(coordinator.lock_id)
            for code in codes:
                if code.expired and code.id is not None:
                    if await coordinator.api.delete_passcode(
                        coordinator.lock_id, code.id
                    ):
                        removed_for_lock.append(code.name)
            if removed_for_lock:
                removed[entity_id] = removed_for_lock

        return {"removed": removed}

    async def handle_configure_autolock(self, call: ServiceCall):
        """Set the autolock seconds."""

        if call.data.get(CONF_ENABLED):
            seconds = call.data.get(CONF_SECONDS) or 10
        else:
            seconds = 0

        for coordinator in self._get_coordinators(call).values():
            if await coordinator.api.set_auto_lock(coordinator.lock_id, seconds):
                coordinator.data.auto_lock_seconds = seconds
                coordinator.async_update_listeners()

    async def handle_list_records(self, call: ServiceCall) -> ServiceResponse:
        """List records for the selected locks."""
        records = {}
        params = {}

        # Convert datetime parameters to millisecond timestamps if provided
        if start_date := call.data.get("start_date"):
            params["start_date"] = int(as_utc(start_date).timestamp() * 1000)
        if end_date := call.data.get("end_date"):
            params["end_date"] = int(as_utc(end_date).timestamp() * 1000)

        # Get pagination values from call data with defaults
        params["page_no"] = call.data.get("page_no", 1)
        params["page_size"] = min(call.data.get("page_size", 50), 200)

        for entity_id, coordinator in self._get_coordinators(call).items():
            lock_records = await coordinator.api.get_lock_records(
                coordinator.lock_id, **params
            )
            records[entity_id] = [
                {
                    "id": record.id,
                    "lock_id": record.lock_id,
                    "record_type": record.record_type.name
                    if record.record_type is not None
                    else None,
                    "success": record.success,
                    "username": record.username,
                    "keyboard_pwd": record.keyboard_pwd,
                    "lock_date": record.lock_date,
                    "server_date": record.server_date,
                }
                for record in lock_records
            ]

        return {"records": records}

    async def handle_update_state(self, call: ServiceCall):
        """Refresh the lock state by calling the coordinator's refresh method."""
        for _entity_id, coordinator in self._get_coordinators(call).items():
            # Set the locked state to none to force the API call.
            coordinator.data.locked = None
            await coordinator.async_refresh()
