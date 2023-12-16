"""Services for ttlock integration."""

from datetime import time
from datetime import datetime
import logging

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, WEEKDAYS
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util.dt import as_utc
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_ALL_DAY,
    CONF_AUTO_UNLOCK,
    CONF_END_TIME,
    CONF_START_TIME,
    CONF_WEEK_DAYS,
    DOMAIN,
)
from .coordinator import LockUpdateCoordinator, coordinator_for
from .models import OnOff, PassageModeConfig, AddPasscodeConfig

_LOGGER = logging.getLogger(__name__)


class Services:
    """Wraps service handlers."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the service singleton."""
        self.hass = hass

    def register(self) -> None:
        """Register services for ttlock integration."""

        self.hass.services.register(
            DOMAIN,
            "configure_passage_mode",
            self.handle_configure_passage_mode,
            vol.Schema(
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

        self.hass.services.register(
            DOMAIN,
            "create_pass_code",
            self.handle_create_pass_code,
            vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required('passcode_name'): cv.string,
                    vol.Required('passcode'): cv.string,
                    vol.Optional('start_time', default=time()): cv.datetime,
                    vol.Optional('end_time', default=time()): cv.datetime,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            "delete_outdated_pass_codes",
            self.handle_delete_outdated_pass_codes,
            vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                }
            ),
        )

    def _get_coordinators(self, call: ServiceCall) -> list[LockUpdateCoordinator]:
        entity_ids = call.data.get(ATTR_ENTITY_ID)
        if entity_ids:
            return [
                coordinator
                for coordinator in [
                    coordinator_for(self.hass, entity_id) for entity_id in entity_ids
                ]
                if coordinator
            ]
        return []

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

        for coordinator in self._get_coordinators(call):
            if await coordinator.api.set_passage_mode(coordinator.lock_id, config):
                coordinator.data.passage_mode_config = config
                coordinator.async_update_listeners()

    async def handle_create_pass_code(self, call: ServiceCall):
        """create a new pass code for the given entities."""

        start_time_val = call.data.get('start_time')
        start_time_utc = as_utc(start_time_val)
        start_time_ts = start_time_utc.timestamp()
        start_time = start_time_ts * 1000
        
        end_time_val = call.data.get('end_time')
        end_time_utc = as_utc(end_time_val)
        end_time_ts = end_time_utc.timestamp()
        end_time = end_time_ts * 1000
                
        pass_code=call.data.get('passcode')
        
        pass_code_name=call.data.get('passcode_name')

        config = AddPasscodeConfig(
            passcode=pass_code,
            passcodeName=pass_code_name,
            startDate=start_time,
            endDate=end_time,
        )

        for coordinator in self._get_coordinators(call):
            if await coordinator.api.add_passcode(coordinator.lock_id, config):
                coordinator.data.add_passcode_config = config
                coordinator.async_update_listeners()

    async def handle_delete_outdated_pass_codes(self, call: ServiceCall):
        """list and delete outdated pass codes for the given entities."""

        for coordinator in self._get_coordinators(call):
            if await coordinator.api.delete_outdated_pass_codes(coordinator.lock_id):
                coordinator.async_update_listeners()