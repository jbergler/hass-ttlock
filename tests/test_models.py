from datetime import datetime, timedelta

from pydantic import BaseModel
import pytest

from custom_components.ttlock.models import (
    Card,
    CardType,
    EpochMs,
    Features,
    Fingerprint,
    Lock,
    OnOff,
    PassageModeConfig,
    Passcode,
)


class TestEpochMs:
    class model_under_test(BaseModel):
        ts: EpochMs

    @pytest.mark.parametrize(
        ("epoch", "tz", "day", "hour"),
        [
            (1675400802000, "Europe/Amsterdam", 5, 6),
            (1675400802000, "Pacific/Auckland", 5, 18),
            (1675400802000, "America/Los_Angeles", 4, 21),
            (1682244497000, "Europe/Amsterdam", 7, 12),
            (1682244497000, "Pacific/Auckland", 7, 22),
            (1682244497000, "America/Los_Angeles", 7, 3),
        ],
    )
    async def test_with_tz(self, hass, epoch, tz, day, hour):
        await hass.config.async_set_time_zone(tz)
        ts = self.model_under_test(ts=epoch).ts
        assert ts.isoweekday() == day
        assert ts.hour == hour


class TestPassageModeConfig:
    def test_passage_mode(self):
        parsed = PassageModeConfig.model_validate(
            {
                "autoUnlock": 2,
                "isAllDay": 2,
                "endDate": 1200,
                "weekDays": [1, 2, 3, 4, 5, 6, 7],
                "passageMode": 1,
                "startDate": 420,
            }
        )
        assert parsed.enabled
        assert not parsed.all_day
        assert not parsed.auto_unlock

    def test_null_start_end_date(self):
        parsed = PassageModeConfig.model_validate(
            {
                "autoUnlock": 2,
                "isAllDay": 1,
                "endDate": None,
                "weekDays": [1, 2, 3, 4, 5, 6, 7],
                "passageMode": 2,
                "startDate": None,
            }
        )
        assert parsed.start_minute == 0
        assert parsed.end_minute == 0


class TestFeatures:
    @pytest.fixture
    def features(self, feature_value):
        return Features.from_feature_value(feature_value)

    @pytest.mark.parametrize(
        ("feature_value", "expected"),
        [
            (
                "10C2F44754CF5F7",
                (
                    Features.lock_remotely,
                    Features.unlock_via_gateway,
                    Features.passage_mode,
                    Features.wifi,
                    Features.door_sensor,
                ),
            ),
            (
                "F44354CD5F3",
                (
                    Features.lock_remotely,
                    Features.unlock_via_gateway,
                    Features.passage_mode,
                ),
            ),
        ],
    )
    def test_flags(self, features: Features, expected):
        for feature in Features:
            assert (feature in features) == (feature in expected)


class TestPasscode:
    def test_permanent_code(self):
        code = Passcode.model_validate(
            {
                "endDate": 0,
                "sendDate": 1690412306000,
                "keyboardPwdId": 311183184,
                "nickName": "Person A",
                "keyboardPwdType": 2,
                "lockId": 4567,
                "keyboardPwdVersion": 4,
                "isCustom": 0,
                "keyboardPwdName": "Person A",
                "keyboardPwd": "1234",
                "startDate": 1690408800000,
                "senderUsername": "some@email.com",
                "receiverUsername": "",
                "status": 1,
            }
        )
        assert code.expired is False

    @pytest.mark.parametrize(
        ("offset", "expired"),
        [
            (timedelta(weeks=-1), True),
            (timedelta(weeks=1), False),
        ],
    )
    def test_temporary_code(self, offset, expired):
        code = Passcode.model_validate(
            {
                "startDate": 1690408800000,
                "endDate": round((datetime.now() + offset).timestamp() * 1000),
                "keyboardPwdId": 311183184,
                "keyboardPwdType": 3,
                "keyboardPwdName": "Person A",
                "keyboardPwd": "1234",
            }
        )
        assert code.expired == expired

    def test_friday_code(self):
        """Test Friday-specific passcode (type 12)."""
        code = Passcode.model_validate(
            {
                "endDate": 1704495600000,
                "sendDate": 1704314923000,
                "keyboardPwdId": 398584274,
                "nickName": "Test User",
                "keyboardPwdType": 12,
                "lockId": 12345,
                "keyboardPwdVersion": 4,
                "isCustom": 0,
                "keyboardPwdName": "Friday Code",
                "keyboardPwd": "123456",
                "startDate": 1704456000000,
                "senderUsername": "test@example.com",
                "receiverUsername": "",
                "status": 2,
            }
        )
        assert code.expired is True  # endDate 1704495600000 is in the past

    @pytest.mark.parametrize(
        ("passcode_type", "offset", "expired"),
        [
            (8, timedelta(weeks=-1), True),  # Monday
            (9, timedelta(weeks=1), False),  # Tuesday
            (10, timedelta(weeks=-1), True),  # Wednesday
            (11, timedelta(weeks=1), False),  # Thursday
            (12, timedelta(weeks=-1), True),  # Friday
            (13, timedelta(weeks=1), False),  # Saturday
            (14, timedelta(weeks=-1), True),  # Sunday
            (1, timedelta(weeks=-1), True),  # One-time
            (4, timedelta(weeks=1), False),  # Cyclic
            (5, timedelta(weeks=-1), True),  # Weekend cyclic
            (7, timedelta(weeks=1), False),  # Workday cyclic
        ],
    )
    def test_time_bounded_codes(self, passcode_type, offset, expired):
        """Test day-specific and other time-bounded passcodes."""
        code = Passcode.model_validate(
            {
                "startDate": 1690408800000,
                "endDate": round((datetime.now() + offset).timestamp() * 1000),
                "keyboardPwdId": 311183184,
                "keyboardPwdType": passcode_type,
                "keyboardPwdName": "Test Code",
                "keyboardPwd": "1234",
            }
        )
        assert code.expired == expired


# All the required fields
MINIMAL_LOCK = {
    "lockId": 1,
    "lockName": "A Lock",
    "lockAlias": "My Lock",
    "lockMac": "00:00:00:00:00:0",
    "date": 0,
    "adminPwd": "1234",
}


class TestLock:
    def test_basic_lock(self):
        lock = Lock.model_validate(
            {
                **MINIMAL_LOCK,
                "lockId": 123,
            }
        )
        assert lock.id == 123

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, OnOff.on),
            (2, OnOff.off),
        ],
    )
    def test_lock_sound(self, value, expected):
        lock = Lock.model_validate(
            {
                **MINIMAL_LOCK,
                "lockSound": value,
            }
        )
        assert lock.lockSound == expected


class TestCard:
    def test_permanent_card_has_no_dates(self):
        card = Card.model_validate(
            {
                "cardId": 1,
                "cardName": "Permanent",
                "cardType": 1,
                "startDate": 0,
                "endDate": 0,
            }
        )
        assert card.type == CardType.normal
        assert card.start_date is None
        assert card.end_date is None
        assert card.expired is False

    def test_expired_card(self):
        past = int((datetime.now() - timedelta(days=1)).timestamp() * 1000)
        card = Card.model_validate({"cardId": 1, "endDate": past})
        assert card.expired is True

    def test_active_card_not_expired(self):
        future = int((datetime.now() + timedelta(days=1)).timestamp() * 1000)
        card = Card.model_validate({"cardId": 1, "endDate": future})
        assert card.expired is False


class TestFingerprint:
    def test_permanent_fingerprint_has_no_dates(self):
        fingerprint = Fingerprint.model_validate(
            {
                "fingerprintId": 1,
                "fingerprintName": "Permanent",
                "fingerprintType": 1,
                "startDate": 0,
                "endDate": 0,
            }
        )
        assert fingerprint.start_date is None
        assert fingerprint.end_date is None
        assert fingerprint.expired is False

    def test_expired_fingerprint(self):
        past = int((datetime.now() - timedelta(days=1)).timestamp() * 1000)
        fingerprint = Fingerprint.model_validate({"fingerprintId": 1, "endDate": past})
        assert fingerprint.expired is True
