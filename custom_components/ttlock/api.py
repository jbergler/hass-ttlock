"""API for TTLock bound to Home Assistant OAuth."""
from abc import ABC, abstractmethod
from enum import IntEnum
from hashlib import md5
import logging
import time
from typing import Any, cast
from urllib.parse import urljoin

from aiohttp import ClientSession

from homeassistant.components.application_credentials import AuthImplementation
from homeassistant.helpers import config_entry_oauth2_flow

_LOGGER = logging.getLogger(__name__)


class BaseApiObject(ABC):
    """Abstract base class for TTLockApi objects."""

    def __init__(self, api: "TTLockApi", data: dict[str, Any]) -> None:
        """Initialize an object form the API."""
        self._api = api
        self._data = data
        _LOGGER.debug(f"{self.__class__.__name__} created with {data}")

    def as_dict(self) -> dict:
        """Serialize for diagnostics."""
        return vars(self)

    @abstractmethod
    async def update(self) -> bool:
        """Poll the API to update ones state."""

    @property
    @abstractmethod
    def id(self) -> int:
        """The API ID for this device."""

    @property
    @abstractmethod
    def name(self) -> str:
        """The configured name for this device."""

    @property
    @abstractmethod
    def mac(self) -> str:
        """The MAC address of the device."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The model of the device."""


class LockState(IntEnum):
    """Locked/unlocked state of a lock."""

    locked = 0
    unlocked = 1
    unknown = 2


def int_to_bool(value: int) -> bool | None:
    """Transform an on/off int value for HA."""
    if value == 1:
        return True
    elif value == 2:
        return False
    else:
        return None


class ApiLock(BaseApiObject):
    """Represents a single lock within an account."""

    _state: LockState = LockState.unknown

    async def update(self) -> bool:
        """Fetch the latest info and state for this lock."""
        data = await self._api.get("lock/detail", lockId=self.id)
        if "lockId" not in data:
            _LOGGER.error(f"Received bad data for {self} {data=}")
            return False

        state = await self._api.get("lock/queryOpenState", lockId=self.id)
        if "state" not in state:
            _LOGGER.error(f"Received bad state for {self} {state=}")
            return False

        if self._data != data or self._state != state["state"]:
            self._data = data
            self._state = LockState(state["state"])
            _LOGGER.debug(f"{self.name} updated: {self._data=} {self._state=}")
            return True

        return False

    @property
    def id(self) -> int:
        """The internal ID of the Lock."""
        return self._data["lockId"]

    @property
    def name(self) -> str:
        """The display name of the lock."""
        return self._data.get("lockAlias", "Unknown")

    @property
    def mac(self) -> str:
        """The mac address of the lock."""
        return self._data.get("lockMac", "00:00:00:00:00:00")

    @property
    def battery_level(self) -> int:
        """The battery level of the lock (0-100)."""
        return self._data.get("electricQuantity", -1)

    @property
    def has_gateway(self) -> bool:
        """If the lock is bound to a gateway."""
        return self._data.get("hasGateway", 0) == 1

    @property
    def sound_enabled(self) -> bool | None:
        """Is the lock configured to make noise?."""
        return int_to_bool(self._data.get("lockSound", 2))

    @property
    def volume(self) -> int | None:
        """Current volume setting (0-5, 0 means off."""
        return self._data.get("soundVolume")

    @property
    def model(self) -> str:
        """The model of the lock."""
        return self._data.get("modelNum") or self.name

    @property
    def version(self) -> str:
        """The firmware version currently reported by the lock."""
        return self._data.get("firmwareRevision", "0.0.0")

    @property
    def state(self) -> LockState:
        """State of the lock (locked/unlocked)."""
        return self._state

    async def lock(self) -> bool:
        """Try to lock the lock."""
        res = await self._api.get("lock/lock", lockId=self.id)

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(f"Failed to lock {self.name}: {res['errmsg']}")
            return False
        else:
            self._state = LockState.locked
            return True

    async def unlock(self) -> bool:
        """Try to unlock the lock."""
        res = await self._api.get("lock/unlock", lockId=self.id)

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(f"Failed to lock {self.name}: {res['errmsg']}")
            return False
        else:
            self._state = LockState.unlocked
            return True


class TTLockAuthImplementation(
    AuthImplementation,
):
    """TTLock Local OAuth2 implementation."""

    async def login(self, username: str, password: str) -> dict:
        """Make a token request."""
        return await self._token_request(
            {
                "username": username,
                "password": md5(password.encode("utf-8")).hexdigest(),
            }
        )

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Resolve the authorization code to tokens."""
        return dict(external_data)


class TTLockApi:
    """Provide TTLock authentication tied to an OAuth2 based config entry."""

    BASE = "https://euapi.ttlock.com/v3/"

    def __init__(
        self,
        websession: ClientSession,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ) -> None:
        """Initialize TTLock auth."""
        self._web_session = websession
        self._oauth_session = oauth_session

    async def async_get_access_token(self) -> str:
        """Return a valid access token."""
        if not self._oauth_session.valid_token:
            await self._oauth_session.async_ensure_token_valid()

        return self._oauth_session.token["access_token"]

    def _add_auth(self, **kwargs) -> dict:
        kwargs["clientId"] = self._oauth_session.implementation.client_id
        kwargs["accessToken"] = self._oauth_session.token["access_token"]
        kwargs["date"] = str(round(time.time() * 1000))
        return kwargs

    async def get(self, path: str, **kwargs: Any):
        """Make GET request to the API with kwargs as query params."""

        url = urljoin(self.BASE, path)
        _LOGGER.debug(f"Sending request to {url} with args={kwargs}")
        resp = await self._web_session.get(
            url,
            params=self._add_auth(**kwargs),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status >= 400 and _LOGGER.isEnabledFor(logging.DEBUG):
            body = await resp.text()
            _LOGGER.debug(f"Request failed: status={resp.status}, {body=}")
        else:
            body = await resp.json()
            _LOGGER.debug(f"RESP: {body=}")

        resp.raise_for_status()
        return cast(dict, await resp.json())

    async def get_locks(self) -> list[ApiLock]:
        """Enumerate all locks in the account."""
        res = await self.get("lock/list", pageNo=1, pageSize=1000)
        return [ApiLock(self, lock) for lock in res["list"]]
