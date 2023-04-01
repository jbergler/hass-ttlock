"""Home Connect entity base class."""

from abc import ABC, abstractmethod
import logging
from typing import Any, Generic, TypeVar

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity

from .api import BaseApiObject, TTLockApi
from .const import DOMAIN, SIGNAL_UPDATE_ENTITIES

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseApiObject)


class TTLockEntity(Entity, Generic[T], ABC):
    """Generic TTLock entity (base class)."""

    _attr_should_poll = False

    def __init__(self, session: TTLockApi, device: T) -> None:
        """Initialize the entity."""
        super().__init__()
        self._session = session
        self.device = device
        self.update_from_data()

    @abstractmethod
    def update_from_data(self) -> None:
        pass

    async def async_update(self) -> None:
        _LOGGER.debug(f"async_update called on {self}")
        await self.device.update()
        self.update_from_data()

    async def async_added_to_hass(self):
        """Register callbacks."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE_ENTITIES, self._update_callback
            )
        )

    @callback
    def _update_callback(self, id):
        """Update data."""
        if id == self.device.id:
            self.async_entity_update()

    @property
    def name(self):
        """Return the name of the node (used for Entity_ID)."""
        return self.device.name

    @property
    def unique_id(self):
        """Return the unique id based on the type and id."""
        return f"{self.device.__class__.__name__}-{self.device.mac}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return info about the device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.mac)},
            manufacturer="TT Lock",
            model=self.device.model,
            name=self.device.name,
            **self.extra_device_info,
        )

    @property
    def extra_device_info(self) -> dict[str, Any]:
        return {}

    @callback
    def async_entity_update(self):
        """Update the entity."""
        _LOGGER.debug("Entity update triggered on %s", self)
        self.async_schedule_update_ha_state(True)
