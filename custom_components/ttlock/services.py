"""Services for ttlock integration."""

from collections.abc import Awaitable, Callable
from datetime import datetime, time
from functools import partial
import logging

import probatio as vol

from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, WEEKDAYS
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.util.dt import as_local, as_utc, now as dt_now

from . import lock_commands
from .ble_protocol import (
    PASSCODE_TYPE_CIRCLE,
    PASSCODE_TYPE_COUNT,
    PASSCODE_TYPE_PERIOD,
    PASSCODE_TYPE_PERMANENT,
)
from .const import (
    AUTO_LOCK_MAX_SECONDS,
    CONF_ALL_DAY,
    CONF_AUTO_UNLOCK,
    CONF_CODE,
    CONF_CODE_TYPE,
    CONF_CREDENTIAL_TYPE,
    CONF_CYCLE_TYPE,
    CONF_END_TIME,
    CONF_SECONDS,
    CONF_SOURCE,
    CONF_START_TIME,
    CONF_WEEK_DAYS,
    DOMAIN,
    SVC_ADD_CARD,
    SVC_CLEANUP_PASSCODES,
    SVC_CLEAR_CARD_SCHEDULE,
    SVC_CLEAR_CREDENTIALS,
    SVC_CLEAR_FINGERPRINT_SCHEDULE,
    SVC_CONFIG_AUTOLOCK,
    SVC_CONFIG_PASSAGE_MODE,
    SVC_CREATE_PASSCODE,
    SVC_DELETE_CARD,
    SVC_DELETE_FINGERPRINT,
    SVC_DELETE_PASSCODE,
    SVC_ENROLL_FINGERPRINT,
    SVC_GET_LOCK_CONFIG,
    SVC_LIST_CARDS,
    SVC_LIST_FINGERPRINTS,
    SVC_LIST_PASSCODES,
    SVC_LIST_RECORDS,
    SVC_MODIFY_CARD_VALIDITY,
    SVC_MODIFY_FINGERPRINT_VALIDITY,
    SVC_MODIFY_PASSCODE,
    SVC_RENAME_CARD,
    SVC_RENAME_FINGERPRINT,
    SVC_SET_CARD_SCHEDULE,
    SVC_SET_FINGERPRINT_SCHEDULE,
    SVC_SYNC_CLOCK,
    SVC_UPDATE_STATE,
)
from .coordinator import LockUpdateCoordinator, NoLocalAdminTransport, coordinator_for
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

# cv.string first: a template rendering only digits arrives as an int.
_PASSCODE = vol.All(cv.string, cv.matches_regex(r"^[0-9]{4,9}\Z"))
_CARD_NUMBER = vol.All(cv.string, cv.matches_regex(r"^[0-9]{1,20}\Z"))
_CYCLE_TYPE = vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF))

# The lock stores a fingerprint number in 6 bytes, and 0 is not a fingerprint.
_FINGERPRINT_NUMBER_MAX = (1 << 48) - 1


def _fingerprint_number(value: object) -> str:
    """Schema validator: a decimal fingerprint number in 1 .. 2**48-1."""
    text = cv.string(value)
    if not text.isdigit() or not 0 < int(text) <= _FINGERPRINT_NUMBER_MAX:
        raise vol.Invalid("fingerprint_number must be a number 1 .. 2**48-1")
    return text


# The passcode types a code can be created as, by the name the services use. The
# lock needs the type again to modify or delete the code.
_CODE_TYPES = {
    "permanent": PASSCODE_TYPE_PERMANENT,
    "period": PASSCODE_TYPE_PERIOD,
    "count": PASSCODE_TYPE_COUNT,
    "cyclic": PASSCODE_TYPE_CIRCLE,
}
_CODE_TYPE_NAMES = {byte: name for name, byte in _CODE_TYPES.items()}

# Whether each type takes a start_time and an end_time. A cyclic code repeats
# daily from the time of day of its start.
_CODE_TYPE_WINDOW = {
    "permanent": (False, False),
    "period": (True, True),
    "count": (True, True),
    "cyclic": (True, False),
}

_CREDENTIAL_TYPES = ("passcodes", "cards", "fingerprints")
_SOURCES = ("cloud", "lock")

_VALIDITY = {
    vol.Required(CONF_START_TIME): cv.datetime,
    vol.Required(CONF_END_TIME): cv.datetime,
}
# At least one day: none would clear a schedule and add nothing in its place.
_DAYS = vol.All(cv.weekdays, vol.Length(min=1))
_WEEKLY_SCHEDULE = {
    vol.Required(CONF_WEEK_DAYS): _DAYS,
    vol.Required(CONF_START_TIME): cv.time,
    vol.Required(CONF_END_TIME): cv.time,
}


def _validate_start_and_end_time_together(config):
    """Ensure start_time and end_time are both present or both absent."""
    has_start = "start_time" in config
    has_end = "end_time" in config

    if has_start != has_end:
        raise vol.Invalid("start_time and end_time must be provided together")

    return config


def _validate_new_passcode(config):
    """Check the window and cycle type against the passcode type."""
    has_window = (CONF_START_TIME in config, CONF_END_TIME in config)
    code_type = config.get(CONF_CODE_TYPE) or (
        "period" if any(has_window) else "permanent"
    )
    if has_window != _CODE_TYPE_WINDOW[code_type]:
        raise vol.Invalid(
            "a permanent code takes no window, a period or count code takes "
            "start_time and end_time, and a cyclic code takes start_time only"
        )
    if (code_type == "cyclic") != (CONF_CYCLE_TYPE in config):
        raise vol.Invalid("cycle_type is required for a cyclic code, and only then")
    return config


def _validate_passcode_change(config):
    """A change by id names the new passcode in full; by code it changes something."""
    if "passcode_id" in config:
        if "passcode_name" not in config or "passcode" not in config:
            raise vol.Invalid("passcode_id needs passcode_name and passcode")
    elif "passcode" not in config and CONF_START_TIME not in config:
        raise vol.Invalid("give a new passcode, a start and end time, or both")
    return config


def _local(value: datetime | None) -> datetime | None:
    """A service datetime as Home Assistant local time; a naive one is local."""
    return as_local(value) if value is not None else None


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _week_days(call: ServiceCall) -> list[int]:
    return [WEEKDAYS.index(day) + 1 for day in call.data[CONF_WEEK_DAYS]]


def _require_cloud(coordinator: LockUpdateCoordinator) -> None:
    """Refuse an action only the cloud can carry while the cloud is off."""
    if not coordinator.cloud_enabled:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cloud_disabled",
            translation_placeholders={"name": coordinator.data.name},
        )


def _read_lock(call: ServiceCall, coordinator: LockUpdateCoordinator) -> bool:
    """Whether a list reads the lock itself rather than the cloud.

    Without a `source` the cloud is read while it is on: it holds names and the
    ids the other actions take.
    """
    source = call.data.get(CONF_SOURCE)
    if source is None:
        return not coordinator.cloud_enabled
    if source == "cloud":
        _require_cloud(coordinator)
    return source == "lock"


async def _local_first[T](
    coordinator: LockUpdateCoordinator,
    local: Callable[[], Awaitable[T]],
    cloud: Callable[[], Awaitable[T]],
) -> T:
    """Run `local`, then `cloud` only when no local transport could carry it."""
    try:
        return await local()
    except NoLocalAdminTransport:
        if not coordinator.cloud_enabled:
            raise
    return await cloud()


async def _set_passage_mode_cloud(
    coordinator: LockUpdateCoordinator, config: PassageModeConfig
) -> None:
    if await coordinator.api.set_passage_mode(coordinator.lock_id, config):
        coordinator.data.passage_mode_config = config
        coordinator.async_update_listeners()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
                    vol.Optional(CONF_AUTO_UNLOCK): cv.boolean,
                    vol.Optional(CONF_ALL_DAY, default=False): cv.boolean,
                    vol.Optional(CONF_START_TIME, default=time()): cv.time,
                    vol.Optional(CONF_END_TIME, default=time()): cv.time,
                    vol.Optional(CONF_WEEK_DAYS, default=WEEKDAYS): _DAYS,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_CREATE_PASSCODE,
            self.handle_create_passcode,
            schema=vol.All(  # ty: ignore[invalid-argument-type] - vol.All is a valid probatio validator
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Required("passcode_name"): cv.string,
                        vol.Required("passcode"): _PASSCODE,
                        vol.Optional("start_time"): cv.datetime,
                        vol.Optional("end_time"): cv.datetime,
                        vol.Optional(CONF_CODE_TYPE): vol.In(_CODE_TYPES),
                        vol.Optional(CONF_CYCLE_TYPE): _CYCLE_TYPE,
                    }
                ),
                _validate_new_passcode,
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_MODIFY_PASSCODE,
            self.handle_modify_passcode,
            schema=vol.All(  # ty: ignore[invalid-argument-type] - vol.All is a valid probatio validator
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Optional("passcode_id"): cv.positive_int,
                        vol.Optional(CONF_CODE): _PASSCODE,
                        vol.Optional("passcode_name"): cv.string,
                        vol.Optional("passcode"): _PASSCODE,
                        vol.Optional("start_time"): cv.datetime,
                        vol.Optional("end_time"): cv.datetime,
                        vol.Optional(CONF_CODE_TYPE, default="permanent"): vol.In(
                            _CODE_TYPES
                        ),
                    }
                ),
                vol.ExactlyOne("passcode_id", CONF_CODE),
                _validate_start_and_end_time_together,
                _validate_passcode_change,
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_DELETE_PASSCODE,
            self.handle_delete_passcode,
            schema=vol.All(  # ty: ignore[invalid-argument-type] - vol.All is a valid probatio validator
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Optional("passcode_id"): cv.positive_int,
                        vol.Optional(CONF_CODE): _PASSCODE,
                        vol.Optional(CONF_CODE_TYPE, default="permanent"): vol.In(
                            _CODE_TYPES
                        ),
                    }
                ),
                vol.ExactlyOne("passcode_id", CONF_CODE),
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
                    vol.Optional(CONF_SOURCE): vol.In(_SOURCES),
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
                    vol.Optional(CONF_SOURCE): vol.In(_SOURCES),
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
                        vol.Coerce(int), vol.Range(min=0, max=AUTO_LOCK_MAX_SECONDS)
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

        self.hass.services.register(
            DOMAIN,
            SVC_LIST_CARDS,
            self.handle_list_cards,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Optional(CONF_SOURCE): vol.In(_SOURCES),
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_RENAME_CARD,
            self.handle_rename_card,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("card_id"): cv.positive_int,
                    vol.Required("name"): cv.string,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_ADD_CARD,
            self.handle_add_card,
            schema=vol.All(  # ty: ignore[invalid-argument-type] - vol.All is a valid probatio validator
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Required("card_number"): _CARD_NUMBER,
                        vol.Required("name"): cv.string,
                        vol.Optional("start_time"): cv.datetime,
                        vol.Optional("end_time"): cv.datetime,
                        vol.Optional("reversed_number", default=False): cv.boolean,
                    }
                ),
                _validate_start_and_end_time_together,
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_DELETE_CARD,
            self.handle_delete_card,
            schema=vol.All(  # ty: ignore[invalid-argument-type] - vol.All is a valid probatio validator
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Optional("card_id"): cv.positive_int,
                        vol.Optional("card_number"): _CARD_NUMBER,
                    }
                ),
                vol.ExactlyOne("card_id", "card_number"),
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_LIST_FINGERPRINTS,
            self.handle_list_fingerprints,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Optional(CONF_SOURCE): vol.In(_SOURCES),
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_RENAME_FINGERPRINT,
            self.handle_rename_fingerprint,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("fingerprint_id"): cv.positive_int,
                    vol.Required("name"): cv.string,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_DELETE_FINGERPRINT,
            self.handle_delete_fingerprint,
            schema=vol.All(  # ty: ignore[invalid-argument-type] - vol.All is a valid probatio validator
                vol.Schema(
                    {
                        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                        vol.Optional("fingerprint_id"): cv.positive_int,
                        vol.Optional("fingerprint_number"): _fingerprint_number,
                    }
                ),
                vol.ExactlyOne("fingerprint_id", "fingerprint_number"),
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_SYNC_CLOCK,
            self.handle_sync_clock,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Optional("time"): cv.datetime,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_CLEAR_CREDENTIALS,
            self.handle_clear_credentials,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required(CONF_CREDENTIAL_TYPE): vol.In(_CREDENTIAL_TYPES),
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_MODIFY_CARD_VALIDITY,
            self.handle_modify_card_validity,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("card_number"): _CARD_NUMBER,
                    **_VALIDITY,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_SET_CARD_SCHEDULE,
            self.handle_set_card_schedule,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("card_number"): _CARD_NUMBER,
                    **_WEEKLY_SCHEDULE,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_CLEAR_CARD_SCHEDULE,
            self.handle_clear_card_schedule,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("card_number"): _CARD_NUMBER,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_ENROLL_FINGERPRINT,
            self.handle_enroll_fingerprint,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Optional("name"): cv.string,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )

        self.hass.services.register(
            DOMAIN,
            SVC_MODIFY_FINGERPRINT_VALIDITY,
            self.handle_modify_fingerprint_validity,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("fingerprint_number"): _fingerprint_number,
                    **_VALIDITY,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_SET_FINGERPRINT_SCHEDULE,
            self.handle_set_fingerprint_schedule,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("fingerprint_number"): _fingerprint_number,
                    **_WEEKLY_SCHEDULE,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_CLEAR_FINGERPRINT_SCHEDULE,
            self.handle_clear_fingerprint_schedule,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
                    vol.Required("fingerprint_number"): _fingerprint_number,
                }
            ),
        )

        self.hass.services.register(
            DOMAIN,
            SVC_GET_LOCK_CONFIG,
            self.handle_get_lock_config,
            schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids}),
            supports_response=SupportsResponse.ONLY,
        )

    def _get_coordinators(self, call: ServiceCall) -> dict[str, LockUpdateCoordinator]:
        """Resolve entity_ids to coordinators; drop unknown ones silently.

        Admin-only handlers call _get_admin_coordinators instead; this
        plain variant is for handlers that stay neutral on access level
        (e.g. update_state, which just forces a refresh).
        """
        entity_ids = call.data.get(ATTR_ENTITY_ID)
        if entity_ids:
            return {
                entity_id: coordinator
                for entity_id in entity_ids
                if (coordinator := coordinator_for(self.hass, entity_id))
            }
        return {}

    def _get_admin_coordinators(
        self, call: ServiceCall
    ) -> dict[str, LockUpdateCoordinator]:
        """Resolve + raise shared_lock_admin_only on any MEMBER target.

        Admin-only services (passcode/card/fingerprint CRUD, passage-mode
        and autolock writes, list_* for admin registries) would otherwise
        get a generic errcode=20002 "not lock admin" from the TTLock
        server. The error names every disallowed entity so the user sees
        them all at once, not one toast per lock.
        """
        resolved = self._get_coordinators(call)
        shared = sorted(
            entity_id
            for entity_id, coord in resolved.items()
            if not coord.access_level.can_admin
        )
        if shared:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="shared_lock_admin_only",
                translation_placeholders={"entities": ", ".join(shared)},
            )
        return resolved

    async def handle_list_passcodes(self, call: ServiceCall) -> ServiceResponse:
        """List all passcodes for the selected locks."""
        passcodes = {}

        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            if _read_lock(call, coordinator):
                records = await coordinator.async_run_local_admin_op(
                    "list passcodes",
                    lock_commands.list_passcodes,
                    idempotent=True,
                    read_only=True,
                )
                passcodes[entity_id] = [
                    {
                        "name": None,
                        "id": None,
                        "passcode": record.passcode,
                        "type": None,
                        "start_date": _iso(record.start),
                        "end_date": _iso(record.end),
                        "expired": None,
                        "new_passcode": record.new_passcode,
                        "code_type": _CODE_TYPE_NAMES.get(record.pwd_type),
                        "cycle_type": record.cycle_type,
                    }
                    for record in records
                ]
                continue
            codes = await coordinator.api.list_passcodes(coordinator.lock_id)
            passcodes[entity_id] = [
                {
                    "name": code.name,
                    "id": code.id,
                    "passcode": code.passcode,
                    "type": code.type.name if code.type is not None else None,
                    "start_date": code.start_date.isoformat()
                    if code.start_date
                    else None,
                    "end_date": code.end_date.isoformat() if code.end_date else None,
                    "expired": code.expired,
                }
                for code in codes
            ]

        return {"passcodes": passcodes}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype

    async def handle_configure_passage_mode(self, call: ServiceCall):
        """Configure passage mode, over a local transport when one can reach the lock."""
        start_time = call.data.get(CONF_START_TIME, time())
        end_time = call.data.get(CONF_END_TIME, time())
        days = [
            WEEKDAYS.index(day) + 1 for day in call.data.get(CONF_WEEK_DAYS, WEEKDAYS)
        ]
        enabled = call.data[CONF_ENABLED]
        all_day = call.data[CONF_ALL_DAY]
        auto_unlock = call.data.get(CONF_AUTO_UNLOCK)
        start_minute = _minutes(start_time)
        end_minute = _minutes(end_time)

        config = PassageModeConfig(
            passageMode=OnOff.on if enabled else OnOff.off,
            autoUnlock=OnOff.on if auto_unlock else OnOff.off,
            isAllDay=OnOff.on if all_day else OnOff.off,
            startDate=start_minute,
            endDate=end_minute,
            weekDays=days,
        )

        for coordinator in self._get_admin_coordinators(call).values():
            local = (
                partial(
                    coordinator.set_passage_mode_local,
                    week_days=days,
                    start_minute=start_minute,
                    end_minute=end_minute,
                    all_day=all_day,
                    auto_unlock=auto_unlock,
                )
                if enabled
                else coordinator.clear_passage_mode_local
            )
            await _local_first(
                coordinator,
                local,
                partial(_set_passage_mode_cloud, coordinator, config),
            )

    async def handle_create_passcode(self, call: ServiceCall):
        """Create a passcode, over a local transport when one can reach the lock."""

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

        code = call.data["passcode"]
        name = call.data["passcode_name"]
        code_type = call.data.get(CONF_CODE_TYPE)

        for coordinator in self._get_admin_coordinators(call).values():
            # The cloud cannot add a count or cyclic code with a chosen value.
            if code_type == "count":
                await coordinator.add_onetime_passcode_local(
                    code,
                    name=name,
                    start=as_local(call.data[CONF_START_TIME]),
                    end=as_local(call.data[CONF_END_TIME]),
                )
            elif code_type == "cyclic":
                await coordinator.add_cyclic_passcode_local(
                    code,
                    name=name,
                    start=as_local(call.data[CONF_START_TIME]),
                    cycle_type=call.data[CONF_CYCLE_TYPE],
                )
            else:
                await _local_first(
                    coordinator,
                    partial(
                        coordinator.add_passcode_local,
                        code,
                        name=name,
                        start=_local(start_time_val),
                        end=_local(end_time_val),
                    ),
                    partial(coordinator.api.add_passcode, coordinator.lock_id, config),
                )

    async def handle_modify_passcode(self, call: ServiceCall):
        """Modify a passcode: by id through the cloud, or by code on the lock."""

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

        for coordinator in self._get_admin_coordinators(call).values():
            if passcode_id is None:
                await coordinator.modify_passcode_local(
                    call.data[CONF_CODE],
                    call.data.get("passcode", ""),
                    start=_local(start_time_val),
                    end=_local(end_time_val),
                    pwd_type=_CODE_TYPES[call.data[CONF_CODE_TYPE]],
                )
                continue
            _require_cloud(coordinator)
            await coordinator.api.modify_passcode(
                coordinator.lock_id, passcode_id, config
            )

    async def handle_delete_passcode(self, call: ServiceCall):
        """Delete a passcode: by id through the cloud, or by code on the lock."""

        passcode_id = call.data.get("passcode_id")

        for coordinator in self._get_admin_coordinators(call).values():
            if passcode_id is None:
                await coordinator.delete_passcode_local(
                    call.data[CONF_CODE],
                    pwd_type=_CODE_TYPES[call.data[CONF_CODE_TYPE]],
                )
                continue
            _require_cloud(coordinator)
            await coordinator.api.delete_passcode(coordinator.lock_id, passcode_id)

    async def handle_cleanup_passcodes(self, call: ServiceCall) -> ServiceResponse:
        """Clean up expired passcodes for the given entities."""
        removed: dict[str, list[str | None]] = {}

        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            _require_cloud(coordinator)
            removed_for_lock: list[str | None] = []
            codes = await coordinator.api.list_passcodes(coordinator.lock_id)
            for code in codes:
                if code.expired and code.id is not None:
                    if await coordinator.api.delete_passcode(
                        coordinator.lock_id, code.id
                    ):
                        removed_for_lock.append(code.name)
            if removed_for_lock:
                removed[entity_id] = removed_for_lock

        return {"removed": removed}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so dict[str, list[str | None]] isn't recognized as a dict[str, JsonValueType] subtype even though every value is one

    async def handle_configure_autolock(self, call: ServiceCall):
        """Set the autolock seconds, over a local transport when one can reach the lock."""

        if call.data.get(CONF_ENABLED):
            seconds = call.data.get(CONF_SECONDS) or 10
        else:
            seconds = 0

        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.set_auto_lock_seconds(seconds)

    async def handle_list_records(self, call: ServiceCall) -> ServiceResponse:
        """List records for the selected locks; the lock's own log newest first."""
        records = {}
        complete = {}
        params = {}

        # Convert datetime parameters to millisecond timestamps if provided
        if start_date := call.data.get("start_date"):
            params["start_date"] = int(as_utc(start_date).timestamp() * 1000)
        if end_date := call.data.get("end_date"):
            params["end_date"] = int(as_utc(end_date).timestamp() * 1000)

        # Get pagination values from call data with defaults
        params["page_no"] = call.data.get("page_no", 1)
        params["page_size"] = min(call.data.get("page_size", 50), 200)

        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            if _read_lock(call, coordinator):
                if start_date or end_date:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="lock_log_dates"
                    )
                log = await coordinator.async_read_operate_log()
                first = (params["page_no"] - 1) * params["page_size"]
                records[entity_id] = [
                    {
                        "id": entry["record_id"],
                        "lock_id": coordinator.lock_id,
                        "record_type": None,
                        "success": None,
                        "username": None,
                        "keyboard_pwd": None,
                        "lock_date": entry["timestamp"],
                        "server_date": None,
                        "lock_record_type": entry["record_type"],
                        "battery": entry["battery"],
                    }
                    for entry in log.records[::-1][first : first + params["page_size"]]
                ]
                complete[entity_id] = log.complete
                continue
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
                    "lock_date": record.lock_date.isoformat()
                    if record.lock_date
                    else None,
                    "server_date": record.server_date.isoformat()
                    if record.server_date
                    else None,
                }
                for record in lock_records
            ]

        if complete:
            return {"records": records, "complete": complete}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype
        return {"records": records}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype

    async def handle_update_state(self, call: ServiceCall):
        """Refresh the lock state by calling the coordinator's refresh method."""
        for coordinator in self._get_coordinators(call).values():
            # Set the locked state to none to force the API call.
            coordinator.data.locked = None
            await coordinator.async_refresh()

    async def handle_list_cards(self, call: ServiceCall) -> ServiceResponse:
        """List all IC cards for the selected locks."""
        cards = {}

        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            if _read_lock(call, coordinator):
                records = await coordinator.async_run_local_admin_op(
                    "list cards",
                    lock_commands.list_cards,
                    idempotent=True,
                    read_only=True,
                )
                cards[entity_id] = [
                    {
                        "id": None,
                        "name": None,
                        "number": record.card_number,
                        "type": None,
                        "sender": None,
                        "start_date": _iso(record.start),
                        "end_date": _iso(record.end),
                        "expired": None,
                    }
                    for record in records
                ]
                continue
            enrolled = await coordinator.api.list_cards(coordinator.lock_id)
            cards[entity_id] = [
                {
                    "id": card.id,
                    "name": card.name,
                    "number": card.number,
                    "type": card.type.name if card.type is not None else None,
                    "sender": card.sender,
                    "start_date": card.start_date.isoformat()
                    if card.start_date
                    else None,
                    "end_date": card.end_date.isoformat() if card.end_date else None,
                    "expired": card.expired,
                }
                for card in enrolled
            ]

        return {"cards": cards}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype

    async def handle_rename_card(self, call: ServiceCall):
        """Rename an IC card for the given entities."""
        card_id = call.data["card_id"]
        name = call.data["name"]

        for coordinator in self._get_admin_coordinators(call).values():
            _require_cloud(coordinator)
            await coordinator.api.rename_card(coordinator.lock_id, card_id, name)

    async def handle_add_card(self, call: ServiceCall) -> ServiceResponse:
        """Add an IC card to the given entities, reporting the outcome per lock."""
        start_ms = end_ms = 0
        if start_time := call.data.get("start_time"):
            start_ms = int(as_utc(start_time).timestamp() * 1000)
        if end_time := call.data.get("end_time"):
            end_ms = int(as_utc(end_time).timestamp() * 1000)

        results: dict[str, dict[str, int | str | None]] = {}
        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            cloud = partial(
                coordinator.api.add_card,
                coordinator.lock_id,
                call.data["card_number"],
                call.data["name"],
                start_ms,
                end_ms,
                call.data["reversed_number"],
            )
            try:
                # Only the cloud reverses a card number; the lock takes it as given.
                if call.data["reversed_number"]:
                    _require_cloud(coordinator)
                    card_id = await cloud()
                else:
                    card_id = await _local_first(
                        coordinator,
                        partial(
                            coordinator.add_card_local,
                            call.data["card_number"],
                            name=call.data["name"],
                            start=_local(start_time),
                            end=_local(end_time),
                        ),
                        cloud,
                    )
            except Exception as err:  # noqa: BLE001 - one failing lock must not hide the others
                _LOGGER.error("Failed to add card for %s: %s", entity_id, err)
                results[entity_id] = {"card_id": None, "error": str(err)}
            else:
                results[entity_id] = {"card_id": card_id, "error": None}

        failed = [e for e, r in results.items() if r["error"] is not None]
        if failed and not call.return_response:
            raise HomeAssistantError(f"Failed to add card for {', '.join(failed)}")

        return {"cards": results}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype

    async def handle_delete_card(self, call: ServiceCall):
        """Delete an IC card: by id through the cloud, or by number on the lock."""
        card_id = call.data.get("card_id")

        for coordinator in self._get_admin_coordinators(call).values():
            if card_id is None:
                await coordinator.delete_card_local(call.data["card_number"])
                continue
            _require_cloud(coordinator)
            await coordinator.api.delete_card(coordinator.lock_id, card_id)

    async def handle_list_fingerprints(self, call: ServiceCall) -> ServiceResponse:
        """List all fingerprints for the selected locks."""
        fingerprints = {}

        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            if _read_lock(call, coordinator):
                records = await coordinator.async_run_local_admin_op(
                    "list fingerprints",
                    lock_commands.list_fingerprints,
                    idempotent=True,
                    read_only=True,
                )
                fingerprints[entity_id] = [
                    {
                        "id": None,
                        "name": None,
                        "number": record.fingerprint_number,
                        "type": None,
                        "sender": None,
                        "start_date": _iso(record.start),
                        "end_date": _iso(record.end),
                        "expired": None,
                    }
                    for record in records
                ]
                continue
            enrolled = await coordinator.api.list_fingerprints(coordinator.lock_id)
            fingerprints[entity_id] = [
                {
                    "id": fingerprint.id,
                    "name": fingerprint.name,
                    "number": fingerprint.number,
                    "type": fingerprint.type.name
                    if fingerprint.type is not None
                    else None,
                    "sender": fingerprint.sender,
                    "start_date": fingerprint.start_date.isoformat()
                    if fingerprint.start_date
                    else None,
                    "end_date": fingerprint.end_date.isoformat()
                    if fingerprint.end_date
                    else None,
                    "expired": fingerprint.expired,
                }
                for fingerprint in enrolled
            ]

        return {"fingerprints": fingerprints}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype

    async def handle_rename_fingerprint(self, call: ServiceCall):
        """Rename a fingerprint for the given entities."""
        fingerprint_id = call.data["fingerprint_id"]
        name = call.data["name"]

        for coordinator in self._get_admin_coordinators(call).values():
            _require_cloud(coordinator)
            await coordinator.api.rename_fingerprint(
                coordinator.lock_id, fingerprint_id, name
            )

    async def handle_delete_fingerprint(self, call: ServiceCall):
        """Delete a fingerprint: by id through the cloud, or by number on the lock."""
        fingerprint_id = call.data.get("fingerprint_id")

        for coordinator in self._get_admin_coordinators(call).values():
            if fingerprint_id is None:
                await coordinator.delete_fingerprint_local(
                    call.data["fingerprint_number"]
                )
                continue
            _require_cloud(coordinator)
            await coordinator.api.delete_fingerprint(
                coordinator.lock_id, fingerprint_id
            )

    async def handle_sync_clock(self, call: ServiceCall) -> None:
        """Set the lock's clock, to `time` or to now."""
        moment = _local(call.data.get("time")) or dt_now()
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.sync_clock(moment)

    async def handle_clear_credentials(self, call: ServiceCall) -> None:
        """Remove every passcode, card or fingerprint from the lock."""
        credential_type = call.data[CONF_CREDENTIAL_TYPE]
        for coordinator in self._get_admin_coordinators(call).values():
            clear = {
                "passcodes": coordinator.clear_passcodes_local,
                "cards": coordinator.clear_cards_local,
                "fingerprints": coordinator.clear_fingerprints_local,
            }[credential_type]
            await clear()

    async def handle_modify_card_validity(self, call: ServiceCall) -> None:
        """Change an IC card's validity window on the lock."""
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.modify_card_validity_local(
                call.data["card_number"],
                as_local(call.data[CONF_START_TIME]),
                as_local(call.data[CONF_END_TIME]),
            )

    async def handle_set_card_schedule(self, call: ServiceCall) -> None:
        """Replace an IC card's weekly schedule on the lock."""
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.set_card_schedule_local(
                call.data["card_number"],
                week_days=_week_days(call),
                start_minute=_minutes(call.data[CONF_START_TIME]),
                end_minute=_minutes(call.data[CONF_END_TIME]),
            )

    async def handle_clear_card_schedule(self, call: ServiceCall) -> None:
        """Clear an IC card's weekly schedule on the lock."""
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.clear_card_cyclic_local(call.data["card_number"])

    async def handle_enroll_fingerprint(self, call: ServiceCall) -> ServiceResponse:
        """Enrol a fingerprint by scanning, and report each lock's new number."""
        results: dict[str, dict[str, str]] = {}
        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            number = await coordinator.enroll_fingerprint_local(
                name=call.data.get("name")
            )
            results[entity_id] = {"number": str(number)}
        return {"fingerprints": results}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype

    async def handle_modify_fingerprint_validity(self, call: ServiceCall) -> None:
        """Change a fingerprint's validity window on the lock."""
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.modify_fingerprint_validity_local(
                call.data["fingerprint_number"],
                as_local(call.data[CONF_START_TIME]),
                as_local(call.data[CONF_END_TIME]),
            )

    async def handle_set_fingerprint_schedule(self, call: ServiceCall) -> None:
        """Replace a fingerprint's weekly schedule on the lock."""
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.set_fingerprint_schedule_local(
                call.data["fingerprint_number"],
                week_days=_week_days(call),
                start_minute=_minutes(call.data[CONF_START_TIME]),
                end_minute=_minutes(call.data[CONF_END_TIME]),
            )

    async def handle_clear_fingerprint_schedule(self, call: ServiceCall) -> None:
        """Clear a fingerprint's weekly schedule on the lock."""
        for coordinator in self._get_admin_coordinators(call).values():
            await coordinator.clear_fingerprint_cyclic_local(
                call.data["fingerprint_number"]
            )

    async def handle_get_lock_config(self, call: ServiceCall) -> ServiceResponse:
        """Read the lock's settings bitmask as named booleans, plus the raw fields."""
        configs: dict[str, dict] = {}
        for entity_id, coordinator in self._get_admin_coordinators(call).items():
            config = await coordinator.async_run_local_admin_op(
                "read lock config",
                lock_commands.get_switch_config,
                idempotent=True,
                read_only=True,
            )
            if config is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="lock_config_unreadable",
                    translation_placeholders={"name": coordinator.data.name},
                )
            configs[entity_id] = {
                "switch_item": config.switch_item,
                "switch_value": config.switch_value,
                **config.settings,
            }
        return {"config": configs}  # ty: ignore[invalid-return-type] - dict/list generics are invariant, so this structurally-JSON-safe dict isn't recognized as a dict[str, JsonValueType] subtype
