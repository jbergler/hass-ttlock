from datetime import datetime, timedelta

from pydantic import BaseModel
import pytest

from custom_components.ttlock.models import (
    AccessLevel,
    Card,
    CardType,
    EpochMs,
    Features,
    Fingerprint,
    Lock,
    LockSummary,
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


class TestLockSummary:
    def _summary(self, **overrides):
        data = {
            "lockId": 1,
            "lockAlias": "Front Door",
            "lockMac": "00:00:00:00:00:00",
            "hasGateway": 0,
            "featureValue": "0",
        }
        data.update(overrides)
        return LockSummary.model_validate(data)

    def test_connectable_via_gateway(self):
        assert self._summary(hasGateway=1).connectable is True

    def test_connectable_via_wifi(self):
        assert self._summary(featureValue=f"{2**56:X}").connectable is True

    def test_not_connectable_without_gateway_or_wifi(self):
        assert self._summary(hasGateway=0, featureValue="0").connectable is False

    def test_mac_is_upper_cased(self):
        """HA matches BLE on upper-case MACs; the cloud isn't consistent about case."""
        assert self._summary(lockMac="16:72:4c:cc:01:c4").mac == "16:72:4C:CC:01:C4"

    def test_lock_data_is_parsed_when_present(self):
        """lock/list is the only endpoint that returns the BLE credential blob."""
        assert self._summary(lockData="Ax9dQ0pF").lockData == "Ax9dQ0pF"

    def test_lock_data_is_none_when_absent(self):
        """Older accounts and trimmed fixtures omit it - that must not be fatal."""
        assert self._summary().lockData is None


class TestAccessLevel:
    def test_top_admin_is_owner(self):
        """userType=110301 on the authenticated account's own ekey."""
        assert AccessLevel.from_ekey("110301", 0) is AccessLevel.OWNER
        assert AccessLevel.from_ekey("110301", 1) is AccessLevel.OWNER

    def test_authorized_shared_key(self):
        """Common ekey (userType=110302) promoted with keyRight=1."""
        assert AccessLevel.from_ekey("110302", 1) is AccessLevel.AUTHORIZED

    def test_plain_shared_key_is_member(self):
        """keyRight=0 on a common ekey is the default share."""
        assert AccessLevel.from_ekey("110302", 0) is AccessLevel.MEMBER

    def test_unknown_user_type_falls_back_to_shared(self):
        """A new TTLock userType value must not misclassify as OWNER."""
        assert AccessLevel.from_ekey("999999", 0) is AccessLevel.MEMBER
        assert AccessLevel.from_ekey("999999", 1) is AccessLevel.AUTHORIZED

    def test_can_admin(self):
        """OWNER and AUTHORIZED can admin; MEMBER cannot."""
        assert AccessLevel.OWNER.can_admin is True
        assert AccessLevel.AUTHORIZED.can_admin is True
        assert AccessLevel.MEMBER.can_admin is False


class TestLockSummaryAccess:
    """access_level/remote_unlock_enabled derived from ekey record."""

    def _summary(self, **overrides):
        data = {
            "lockId": 1,
            "lockAlias": "Front Door",
            "lockMac": "00:00:00:00:00:00",
            "hasGateway": 1,
            "featureValue": "0",
        }
        data.update(overrides)
        return LockSummary.model_validate(data)

    def test_owner_from_user_type(self):
        assert (
            self._summary(userType="110301", keyRight=0).access_level
            is AccessLevel.OWNER
        )

    def test_member_default(self):
        """No userType (older summaries) classifies as MEMBER - never OWNER."""
        assert self._summary().access_level is AccessLevel.MEMBER

    def test_authorized_from_key_right(self):
        assert (
            self._summary(userType="110302", keyRight=1).access_level
            is AccessLevel.AUTHORIZED
        )

    def test_remote_unlock_enabled_matches_wire_value(self):
        """remoteEnable is 1=yes, 2=no on the API; default is 2."""
        assert self._summary(remoteEnable=1).remote_unlock_enabled is True
        assert self._summary(remoteEnable=2).remote_unlock_enabled is False
        assert self._summary().remote_unlock_enabled is False

    def test_is_valid_now_unbounded_is_always_valid(self):
        """startDate=0 and endDate=0 mean 'unbounded on that side'."""
        assert self._summary(startDate=0, endDate=0).is_valid_now(
            1_700_000_000_000
        ) == (
            True,
            None,
        )

    def test_is_valid_now_before_start(self):
        valid, reason = self._summary(startDate=2_000_000_000_000).is_valid_now(
            1_000_000_000_000
        )
        assert valid is False
        assert reason == "shared_lock_not_yet_valid"

    def test_is_valid_now_after_end(self):
        valid, reason = self._summary(endDate=1_000_000_000_000).is_valid_now(
            2_000_000_000_000
        )
        assert valid is False
        assert reason == "shared_lock_expired"

    def test_is_valid_now_frozen_shadows_window(self):
        """Frozen beats everything: don't claim the window excluded the user."""
        valid, reason = self._summary(
            keyStatus="110405", startDate=0, endDate=0
        ).is_valid_now(1_700_000_000_000)
        assert valid is False
        assert reason == "shared_lock_frozen"

    def test_is_frozen_matches_wire_status(self):
        assert self._summary(keyStatus="110405").is_frozen is True
        assert self._summary(keyStatus="110401").is_frozen is False
        assert self._summary().is_frozen is False
