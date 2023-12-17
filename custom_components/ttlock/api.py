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

from .models import AddPasscodeConfig, Features, Lock, LockState, PassageModeConfig

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
        return Lock.parse_obj(res)

    async def get_lock_state(self, lock_id: int) -> LockState:
        """Get the state of a lock."""
        async with GW_LOCK:
            res = await self.get("lock/queryOpenState", lockId=lock_id)
        return LockState.parse_obj(res)

    async def get_lock_passage_mode_config(self, lock_id: int) -> PassageModeConfig:
        """Get the passage mode configuration of a lock."""
        res = await self.get("lock/getPassageModeConfig", lockId=lock_id)
        return PassageModeConfig.parse_obj(res)

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
            res = await self.post(
                "keyboardPwd/add",
                lockId=lock_id,
                addType=2,  # via gateway
                keyboardPwd=config.passcode,
                keyboardPwdName=config.passcode_name,
                keyboardPwdType=3,  # Only temporary passcode supported
                startDate=config.start_minute,
                endDate=config.end_minute,
            )

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error(
                "Failed to create passcode for %s: %s", lock_id, res["errmsg"]
            )
            return False

        return True

    async def delete_outdated_pass_codes(self, lock_id: int) -> bool:
        """Get the list of pass codes of a lock."""
        res = await self.get(
            "lock/listKeyboardPwd", lockId=lock_id, pageNo=1, pageSize=100
        )

        def passcode_outdated(passcode) -> bool:
            is_temporary = passcode.get("keyboardPwdType") == 3
            is_outdated = passcode.get("endDate") < int(round(time.time() * 1000))
            return is_temporary and is_outdated

        for passcode in res["list"]:
            if passcode_outdated(passcode):
                async with GW_LOCK:
                    resDel = await self.post(
                        "keyboardPwd/delete",
                        lockId=lock_id,
                        deleteType=2,  # via gateway
                        keyboardPwdId=passcode.get("keyboardPwdId"),
                    )

                if "errcode" in resDel and resDel["errcode"] != 0:
                    _LOGGER.error(
                        "Failed to delete passcodes for %s: %s",
                        lock_id,
                        resDel["errmsg"],
                    )
                    return False

        if "errcode" in res and res["errcode"] != 0:
            _LOGGER.error("Failed to list passcodes for %s: %s", lock_id, res["errmsg"])
            return False

        return True
