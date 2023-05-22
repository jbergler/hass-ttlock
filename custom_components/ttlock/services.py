"""Services for ttlock integration."""

from datetime import time
import logging

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, WEEKDAYS
from homeassistant.core import HomeAssistant, ServiceCall
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
from .models import OnOff, PassageModeConfig

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
