"""Models for parsing the TTLock API data."""

from collections import namedtuple
from datetime import datetime
from enum import Enum, IntEnum, IntFlag, auto

from pydantic import BaseModel, Field, GetCoreSchemaHandler, field_validator
from pydantic_core import core_schema

from homeassistant.util import dt


class EpochMs(datetime):
    """Parse millisecond epoch into a local datetime."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler: GetCoreSchemaHandler):
        """Return pydantic core schema."""
        return core_schema.no_info_plain_validator_function(cls.validate)

    @classmethod
    def validate(cls, v):
        """Use homeassistant time helpers to parse epoch."""
        return dt.as_local(dt.utc_from_timestamp(v / 1000))


class OnOff(Enum):
    """Tri-state bool for fields that are on/off."""

    unknown = 0
    on = 1
    off = 2

    def __bool__(self) -> bool:
        """Overload truthyness to 'on'."""
        return self == OnOff.on


class OpenDirection(Enum):
    """Tri-state for door open direction."""

    unknown = 0
    left = 1
    right = 2


class State(Enum):
    """State of the lock."""

    locked = 0
    unlocked = 1
    unknown = 2


class SensorState(Enum):
    """State of the sensor."""

    opened = 0
    closed = 1
    unknown = None


class Lock(BaseModel):
    """Lock details."""

    id: int = Field(..., alias="lockId")
    type: str = Field(..., alias="lockName")
    name: str = Field("Lock", alias="lockAlias")
    mac: str = Field(..., alias="lockMac")
    battery_level: int | None = Field(None, alias="electricQuantity")
    featureValue: str | None = None
    timezoneRawOffset: int = 0
    model: str | None = Field(None, alias="modelNum")
    hardwareRevision: str | None = None
    firmwareRevision: str | None = None
    autoLockTime: int | None = Field(None, alias="autoLockTime")
    lockSound: OnOff = OnOff.unknown
    privacyLock: OnOff = OnOff.unknown
    tamperAlert: OnOff = OnOff.unknown
    resetButton: OnOff = OnOff.unknown
    openDirection: OpenDirection = OpenDirection.unknown
    passageMode: OnOff = OnOff.unknown
    passageModeAutoUnlock: OnOff = OnOff.unknown
    date: int

    # sensitive fields
    noKeyPwd: str = Field(alias="adminPwd")


class Sensor(BaseModel):
    """sensor details."""

    id: int = Field(..., alias="doorSensorId")
    name: str = Field("Sensor", alias="name")
    battery_level: int | None = Field(None, alias="electricQuantity")
    mac: str = Field(..., alias="mac")


class LockState(BaseModel):
    """Lock state."""

    locked: State | None = Field(State.unknown, alias="state")
    opened: SensorState | None = Field(SensorState.unknown, alias="sensorState")


class PassageModeConfig(BaseModel):
    """The passage mode configuration of the lock."""

    enabled: OnOff = Field(OnOff.unknown, alias="passageMode")
    start_minute: int = Field(0, alias="startDate")
    end_minute: int = Field(0, alias="endDate")
    all_day: OnOff = Field(OnOff.unknown, alias="isAllDay")
    week_days: list[int] = Field([], alias="weekDays")  # monday = 1, sunday = 7
    auto_unlock: OnOff = Field(OnOff.unknown, alias="autoUnlock")

    @field_validator("start_minute", mode="before")
    @classmethod
    def _set_start_minute(cls, start_minute: int | None) -> int:
        return start_minute or 0

    @field_validator("end_minute", mode="before")
    @classmethod
    def _set_end_minute(cls, end_minute: int | None) -> int:
        return end_minute or 0


class PasscodeType(IntEnum):
    """Type of passcode.

    Reference: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fpasscode%2FgetEn.html
    """

    unknown = 0
    one_time = 1  # Only valid for once within 6 hours from the Start Time
    permanent = 2  # Must be used at least once within 24 hours after Start Time
    period = 3  # Must be used at least once within 24 hours after Start Time
    delete = 4  # This code will delete all other codes
    weekends = 5  # Valid during time period at the weekend
    daily = 6  # Valid during time period everyday
    weekdays = 7  # Valid during time period on workdays
    mondays = 8  # Valid during time period on Mondays
    tuesdays = 9  # Valid during time period on Tuesdays
    wednesdays = 10  # Valid during time period on Wednesdays
    thursdays = 11  # Valid during time period on Thursdays
    fridays = 12  # Valid during time period on Fridays
    saturdays = 13  # Valid during time period on Saturdays
    sundays = 14  # Valid during time period on Sundays

    # Backward compatibility alias
    temporary = 3  # Alias for period


class Passcode(BaseModel):
    """A single passcode on a lock."""

    id: int | None = Field(None, alias="keyboardPwdId")
    passcode: str | None = Field(None, alias="keyboardPwd")
    name: str | None = Field(None, alias="keyboardPwdName")
    type: PasscodeType | None = Field(None, alias="keyboardPwdType")
    start_date: EpochMs | None = Field(None, alias="startDate")
    end_date: EpochMs | None = Field(None, alias="endDate")

    @property
    def expired(self) -> bool:
        """True if the passcode expired."""
        # Time-bounded passcode types that can expire
        time_bounded_types = (
            PasscodeType.period,
            PasscodeType.one_time,
            PasscodeType.delete,
            PasscodeType.weekends,
            PasscodeType.daily,
            PasscodeType.weekdays,
            PasscodeType.mondays,
            PasscodeType.tuesdays,
            PasscodeType.wednesdays,
            PasscodeType.thursdays,
            PasscodeType.fridays,
            PasscodeType.saturdays,
            PasscodeType.sundays,
        )
        if self.type in time_bounded_types and self.end_date:
            return self.end_date < dt.now()

        # Assume not expired for other types
        return False


class RecordType(IntEnum):
    """Type of lock record."""

    BLUETOOTH_UNLOCK = 1
    PASSWORD_UNLOCK = 4
    PARKING_LOCK = 5
    PARKING_SPACE_LOCK_AND_LOWERING = 6
    IC_CARD_UNLOCK = 7
    FINGERPRINT_UNLOCK = 8
    BRACELET_UNLOCK = 9
    MECHANICAL_KEY_UNLOCK = 10
    BLUETOOTH_LOCK = 11
    GATEWAY_UNLOCK = 12
    ILLEGAL_UNLOCKING = 29
    DOOR_MAGNET_CLOSED = 30
    DOOR_SENSOR_OPEN = 31
    OPEN_DOOR_FROM_INSIDE = 32
    FINGERPRINT_LOCK = 33
    PASSWORD_LOCK = 34
    IC_CARD_LOCK = 35
    MECHANICAL_KEY_LOCK = 36
    APP_BUTTON_CONTROL = 37
    POST_OFFICE_LOCAL_MAIL = 42
    POST_OFFICE_OUT_OF_TOWN_MAIL = 43
    ANTI_THEFT_ALARM = 44
    AUTOMATIC_LOCK_TIMEOUT = 45
    UNLOCK_BUTTON = 46
    LOCK_BUTTON = 47
    SYSTEM_LOCKED = 48
    HOTEL_CARD_UNLOCK = 49
    HIGH_TEMPERATURE_UNLOCK = 50
    DELETED_CARD_UNLOCK = 51
    LOCK_WITH_APP = 52
    LOCK_WITH_PASSWORD = 53
    CAR_LEAVES = 54
    REMOTE_CONTROL = 55
    QR_CODE_UNLOCK_SUCCESS = 57
    QR_CODE_UNLOCK_FAILED_EXPIRED = 58
    OPEN_ANTI_LOCK = 59
    CLOSE_ANTI_LOCK = 60
    QR_CODE_LOCK_SUCCESS = 61
    QR_CODE_UNLOCK_FAILED_LOCKED = 62
    AUTOMATIC_UNLOCKING_NORMAL_OPEN_TIME = 63
    DOOR_NOT_CLOSED_ALARM = 64
    UNLOCK_TIMEOUT = 65
    LOCKOUT_TIMEOUT = 66
    THREE_D_FACE_UNLOCK_SUCCESS = 67
    THREE_D_FACE_UNLOCK_FAILED_LOCKED = 68
    THREE_D_FACE_LOCK = 69
    THREE_D_FACE_RECOGNITION_FAILED_EXPIRED = 71
    APP_AUTHORIZATION_BUTTON_UNLOCK_SUCCESS = 75
    GATEWAY_AUTHORIZATION_KEY_UNLOCK_SUCCESS = 76
    DUAL_AUTHENTICATION_BLUETOOTH_UNLOCK_SUCCESS = 77
    DUAL_AUTHENTICATION_PASSWORD_UNLOCK_SUCCESS = 78
    DUAL_AUTHENTICATION_FINGERPRINT_UNLOCK_SUCCESS = 79
    DUAL_AUTHENTICATION_IC_CARD_UNLOCK_SUCCESS = 80
    DUAL_AUTHENTICATION_FACE_CARD_UNLOCK_SUCCESS = 81
    DUAL_AUTHENTICATION_REMOTE_UNLOCK_SUCCESS = 82
    DUAL_AUTHENTICATION_PALM_VEIN_UNLOCK_SUCCESS = 83
    PALM_VEIN_UNLOCK_SUCCESS = 84
    PALM_VEIN_UNLOCK_FAILED_LOCKED = 85
    PALM_VEIN_ATRESIA = 86
    PALM_VEIN_OPENING_FAILED_EXPIRED = 88
    IC_CARD_UNLOCK_FAILED = 91
    ADMINISTRATOR_PASSWORD_UNLOCK = 92


class LockRecord(BaseModel):
    """A single record entry from a lock."""

    id: int | None = Field(None, alias="recordId")
    lock_id: int | None = Field(None, alias="lockId")
    record_type: RecordType | None = Field(None, alias="recordType")
    success: bool = Field(...)
    username: str | None = Field(None)
    keyboard_pwd: str | None = Field(None, alias="keyboardPwd")
    lock_date: EpochMs | None = Field(None, alias="lockDate")
    server_date: EpochMs | None = Field(None, alias="serverDate")


class AddPasscodeConfig(BaseModel):
    """The passcode creation configuration."""

    passcode: str | None = Field(None, alias="passcode")
    passcode_name: str | None = Field(None, alias="passcodeName")
    start_minute: int | None = Field(None, alias="startDate")
    end_minute: int | None = Field(None, alias="endDate")


class Action(Enum):
    """Lock action from an event."""

    unknown = auto()
    lock = auto()
    unlock = auto()
    open = auto()
    close = auto()


EventDescription = namedtuple("EventDescription", ["action", "description"])


class Event:
    """Event description for lock events."""

    def __init__(self, event_id: int):
        """Initialize from int event id."""
        self._value_ = event_id

    EVENTS: dict[int, EventDescription] = {
        1: EventDescription(Action.unlock, "unlock by app"),
        4: EventDescription(Action.unlock, "unlock by passcode"),
        7: EventDescription(Action.unlock, "unlock by IC card"),
        8: EventDescription(Action.unlock, "unlock by fingerprint"),
        9: EventDescription(Action.unlock, "unlock by wrist strap"),
        10: EventDescription(Action.unlock, "unlock by Mechanical key"),
        11: EventDescription(Action.lock, "lock by app"),
        12: EventDescription(Action.unlock, "unlock by gateway"),
        29: EventDescription(Action.unknown, "apply some force on the Lock"),
        30: EventDescription(Action.close, "Door sensor closed"),
        31: EventDescription(Action.open, "Door sensor open"),
        32: EventDescription(Action.open, "open from inside"),
        33: EventDescription(Action.lock, "lock by fingerprint"),
        34: EventDescription(Action.lock, "lock by passcode"),
        35: EventDescription(Action.lock, "lock by IC card"),
        36: EventDescription(Action.lock, "lock by Mechanical key"),
        37: EventDescription(Action.unknown, "Remote Control"),
        42: EventDescription(Action.unknown, "received new local mail"),
        43: EventDescription(Action.unknown, "received new other cities' mail"),
        44: EventDescription(Action.unknown, "Tamper alert"),
        45: EventDescription(Action.lock, "Auto Lock"),
        46: EventDescription(Action.unlock, "unlock by unlock key"),
        47: EventDescription(Action.lock, "lock by lock key"),
        48: EventDescription(
            Action.unknown,
            "System locked ( Caused by, for example: Using INVALID Passcode/Fingerprint/Card several times)",
        ),
        49: EventDescription(Action.unlock, "unlock by hotel card"),
        50: EventDescription(Action.unlock, "unlocked due to the high temperature"),
        51: EventDescription(Action.unknown, "Try to unlock with a deleted card"),
        52: EventDescription(Action.unknown, "Dead lock with APP"),
        53: EventDescription(Action.unknown, "Dead lock with passcode"),
        54: EventDescription(Action.unknown, "The car left (for parking lock)"),
        55: EventDescription(Action.unlock, "unlock with key fob"),
        57: EventDescription(Action.unlock, "unlock with QR code success"),
        58: EventDescription(
            Action.unknown, "Unlock with QR code failed, it's expired"
        ),
        59: EventDescription(Action.unknown, "Double locked"),
        60: EventDescription(Action.unknown, "Cancel double lock"),
        61: EventDescription(Action.lock, "Lock with QR code success"),
        62: EventDescription(
            Action.unknown, "Lock with QR code failed, the lock is double locked"
        ),
        63: EventDescription(Action.unlock, "auto unlock at passage mode"),
        67: EventDescription(Action.unlock, "3D Face Unlock Success"),
        68: EventDescription(Action.unknown, "3D Face Unlock Failed (Locked)"),
        69: EventDescription(Action.lock, "Locked via 3D Face"),
        71: EventDescription(Action.unknown, "3D Face Recognition Failed (Expired)"),
    }

    @property
    def _info(self) -> EventDescription:
        return self.EVENTS.get(
            self._value_, EventDescription(Action.unknown, "unknown")
        )

    @property
    def action(self) -> Action:
        """The action this event represents."""
        return self._info.action

    @property
    def description(self) -> str:
        """A description of the event."""
        return self._info.description

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler: GetCoreSchemaHandler):
        """Return pydantic core schema."""
        return core_schema.no_info_plain_validator_function(cls.validate)

    @classmethod
    def validate(cls, v):
        """Validate for pydantic type."""
        if not isinstance(v, int):
            raise TypeError("int required")

        if v not in cls.EVENTS:
            raise ValueError("invalid record")

        return cls(v)

    def __repr__(self):
        """Representation of the event."""
        return f"Event({self._info})"


class WebhookEvent(BaseModel):
    """Event from the API (via webhook)."""

    id: int = Field(..., alias="lockId")
    mac: str = Field(..., alias="lockMac")
    battery_level: int | None = Field(None, alias="electricQuantity")
    server_ts: EpochMs = Field(..., alias="serverDate")
    lock_ts: EpochMs = Field(..., alias="lockDate")
    event: Event = Field(..., alias="recordType")
    user: str | None = Field(None, alias="username")
    success: bool

    # keyboardPwd - ignore for now

    @property
    def state(self) -> LockState:
        """The end state of the lock after this event."""
        if self.success and self.event.action == Action.lock:
            return LockState(state=State.locked)
        elif self.success and self.event.action == Action.unlock:
            return LockState(state=State.unlocked)
        return LockState(state=None)

    @property
    def sensorState(self) -> LockState:
        """The end state of the sensor after this event."""
        if self.success and self.event.action == Action.close:
            return LockState(state=State.locked, sensorState=SensorState.closed)
        elif self.success and self.event.action == Action.open:
            return LockState(sensorState=SensorState.opened)
        return LockState(sensorState=None)


class Features(IntFlag):
    """Parses the features bitmask from the hex string in the api response."""

    # Docs: https://euopen.ttlock.com/document/doc?urlName=cloud%2Flock%2FfeatureValueEn.html.

    lock_remotely = 2**8
    unlock_via_gateway = 2**10
    door_sensor = 2**13
    passage_mode = 2**22
    wifi = 2**56

    @classmethod
    def from_feature_value(cls, value: str | None):
        """Parse the hex feature_value string."""
        return Features(int(value, 16)) if value else Features(0)
