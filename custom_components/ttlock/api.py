"""API for TTLock bound to Home Assistant OAuth."""

import asyncio
from collections.abc import Mapping
from hashlib import md5
import json
import logging
from secrets import token_hex
import time
from typing import Any, cast
from urllib.parse import urljoin

from aiohttp import ClientResponse, ClientSession

from homeassistant.components.application_credentials import AuthImplementation
from homeassistant.helpers import config_entry_oauth2_flow

from .models import (
    AddPasscodeConfig,
    Features,
    Lock,
    LockRecord,
    LockState,
    PassageModeConfig,
    Passcode,
    Sensor,
)

_LOGGER = logging.getLogger(__name__)
GW_LOCK = asyncio.Lock()


class RequestFailed(Exception):
    """Exception when TTLock API returns an error."""

    pass


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

    async def _async_refresh_token(self, token: dict) -> dict:
        """Refresh tokens."""
        new_token = await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": token["refresh_token"],
            }
        )
        return {**token, **new_token}

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

    async def _add_auth(self, **kwargs) -> dict:
        kwargs["clientId"] = self._oauth_session.implementation.client_id
        kwargs["accessToken"] = await self.async_get_access_token()
        kwargs["date"] = str(round(time.time() * 1000))
        return kwargs

    async def _parse_resp(self, resp: ClientResponse, log_id: str) -> Mapping[str, Any]:
        if resp.status >= 400:
            body = await resp.text()
            _LOGGER.debug(
                "[%s] Request failed: status=%s, body=%s", log_id, resp.status, body
            )
        else:
            body = await resp.json()
            _LOGGER.debug(
                "[%s] Received response: status=%s: body=%s", log_id, resp.status, body
            )

        resp.raise_for_status()

        res = cast(dict, await resp.json())
        if res.get("errcode", 0) != 0:
            _LOGGER.debug("[%s] API returned: %s", log_id, res)
            raise RequestFailed(f"API returned: {res}")

        return cast(dict, await resp.json())

    async def get(self, path: str, **kwargs: Any) -> Mapping[str, Any]:
        """Make GET request to the API with kwargs as query params."""
        log_id = token_hex(2)

        url = urljoin(self.BASE, path)
        _LOGGER.debug("[%s] Sending request to %s with args=%s", log_id, url, kwargs)
        resp = await self._web_session.get(
            url,
            params=await self._add_auth(**kwargs),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return await self._parse_resp(resp, log_id)

    async def post(self, path: str, **kwargs: Any) -> Mapping[str, Any]:
        """Make GET request to the API with kwargs as query params."""
        log_id = token_hex(2)

        url = urljoin(self.BASE, path)
        _LOGGER.debug("[%s] Sending request to %s with args=%s", log_id, url, kwargs)
        resp = await self._web_session.post(
            url,
            params=await self._add_auth(),
            data=kwargs,
        )
        return await self._parse_resp(resp, log_id)

    async def get_locks(self) -> list[int]:
        """Enumerate all locks in the account."""
        res = await self.get("lock/list", pageNo=1, pageSize=1000)

        def lock_connectable(lock) -> bool:
            has_gateway = lock.get("hasGateway") != 0
            has_wifi = Features.wifi in Features.from_feature_value(
                lock.get("featureValue")
            )
            return has_gateway or has_wifi

        return [lock["lockId"] for lock in res["list"] if lock_connectable(lock)]

    async def get_lock(self, lock_id: int) -> Lock:
        """Get a lock by ID."""
        res = await self.get("lock/detail", lockId=lock_id)
        return Lock.model_validate(res)

    async def get_sensor(self, lock_id: int) -> Sensor | None:
        """Get the Sensor."""

        try:
            res = await self.get("doorSensor/query", lockId=lock_id)
            return Sensor.model_validate(res)
        except RequestFailed:
            # Janky but the API doesn't return different errors if the sensor is missing or there's some other problem
            return None

    async def get_lock_state(self, lock_id: int) -> LockState:
        """Get the state of a lock."""
        async with GW_LOCK:
            res = await self.get("lock/queryOpenState", lockId=lock_id)
        return LockState.model_validate(res)

    async def get_lock_passage_mode_config(self, lock_id: int) -> PassageModeConfig:
        """Get the passage mode configuration of a lock."""
        res = await self.get("lock/getPassageModeConfig", lockId=lock_id)
        return PassageModeConfig.model_validate(res)

    async def lock(self, lock_id: int) -> bool:
        """Try to lock the lock."""
        async with GW_LOCK:
            res = await self.get("lock/lock", lockId=lock_id)

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error("Failed to lock %s: %s", lock_id, res["errmsg"])
            return False

        return True

    async def unlock(self, lock_id: int) -> bool:
        """Try to unlock the lock."""
        async with GW_LOCK:
            res = await self.get("lock/unlock", lockId=lock_id)

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error("Failed to unlock %s: %s", lock_id, res["errmsg"])
            return False

        return True

    async def set_passage_mode(self, lock_id: int, config: PassageModeConfig) -> bool:
        """Configure passage mode."""

        async with GW_LOCK:
            res = await self.post(
                "lock/configPassageMode",
                lockId=lock_id,
                type=2,  # via gateway
                passageMode=1 if config.enabled else 2,
                autoUnlock=1 if config.auto_unlock else 2,
                isAllDay=1 if config.all_day else 2,
                startDate=config.start_minute,
                endDate=config.end_minute,
                weekDays=json.dumps(config.week_days),
            )

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error("Failed to unlock %s: %s", lock_id, res["errmsg"])
            return False

        return True

    async def add_passcode(self, lock_id: int, config: AddPasscodeConfig) -> bool:
        """Add new passcode."""

        async with GW_LOCK:
            params: dict[str, Any] = {
                "lockId": lock_id,
                "addType": 2,  # via gateway
                "keyboardPwd": config.passcode,
                "keyboardPwdName": config.passcode_name,
            }

            # Only include dates and set type to temporary if dates are provided
            if config.start_minute is not None or config.end_minute is not None:
                params["keyboardPwdType"] = 3  # temporary passcode
                if config.start_minute is not None:
                    params["startDate"] = config.start_minute
                if config.end_minute is not None:
                    params["endDate"] = config.end_minute
            else:
                params["keyboardPwdType"] = 2  # permanent passcode

            res = await self.post("keyboardPwd/add", **params)

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(
                "Failed to create passcode for %s: %s", lock_id, res["errmsg"]
            )
            return False

        return True

    async def modify_passcode(
        self, lock_id: int, passcode_id: int, config: AddPasscodeConfig
    ) -> bool:
        """Modify an existing passcode."""

        async with GW_LOCK:
            params: dict[str, Any] = {
                "lockId": lock_id,
                "changeType": 2,  # via gateway
                "keyboardPwdId": passcode_id,
            }

            # Only include fields being changed (None values are omitted per API spec)
            if config.passcode is not None:
                params["newKeyboardPwd"] = config.passcode
            if config.passcode_name is not None:
                params["keyboardPwdName"] = config.passcode_name
            if config.start_minute is not None:
                params["startDate"] = config.start_minute
            if config.end_minute is not None:
                params["endDate"] = config.end_minute

            res = await self.post("keyboardPwd/change", **params)

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(
                "Failed to modify passcode for %s: %s", lock_id, res["errmsg"]
            )
            return False

        return True

    async def list_passcodes(self, lock_id: int) -> list[Passcode]:
        """Get currently configured passcodes from lock."""

        res = await self.get(
            "lock/listKeyboardPwd", lockId=lock_id, pageNo=1, pageSize=100
        )
        return [Passcode.model_validate(passcode) for passcode in res["list"]]

    async def delete_passcode(self, lock_id: int, passcode_id: int) -> bool:
        """Delete a passcode from lock."""

        async with GW_LOCK:
            resDel = await self.post(
                "keyboardPwd/delete",
                lockId=lock_id,
                deleteType=2,  # via gateway
                keyboardPwdId=passcode_id,
            )

        if "errcode" in resDel and resDel["errcode"] != 0:
            _LOGGER.error(
                "Failed to delete passcode for %s: %s",
                lock_id,
                resDel["errmsg"],
            )
            return False

        return True

    async def set_auto_lock(self, lock_id: int, seconds: int) -> bool:
        """Set the AutoLock feature of the lock."""

        async with GW_LOCK:
            res = await self.post(
                "lock/setAutoLockTime",
                lockId=lock_id,
                seconds=seconds,
                type=2,
            )

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(
                "Failed to update autolock",
                lock_id,
                res["errmsg"],
            )
            return False

        return True

    async def set_lock_sound(self, lock_id: int, value: int) -> bool:
        """Set the LockSound feature of the lock."""

        async with GW_LOCK:
            res = await self.post(
                "lock/updateSetting",
                lockId=lock_id,
                value=value,
                type=6,
                changeType=2,
            )

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(
                "Failed to update sound setting",
                lock_id,
                res["errmsg"],
            )
            return False

        return True

    async def get_lock_records(
        self,
        lock_id: int,
        start_date: int | None = None,
        end_date: int | None = None,
        page_no: int = 1,
        page_size: int = 20,
    ) -> list[LockRecord]:
        """Get the operation records for a lock."""
        params = {
            "lockId": lock_id,
            "pageNo": page_no,
            "pageSize": min(page_size, 200),
            "date": str(round(time.time() * 1000)),
        }

        if start_date is not None:
            params["startDate"] = start_date
        if end_date is not None:
            params["endDate"] = end_date

        res = await self.get("lockRecord/list", **params)

        # Serialize each record to ensure datetime objects are handled
        return [LockRecord.model_validate(record) for record in res["list"]]
