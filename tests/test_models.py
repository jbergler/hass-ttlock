from pydantic import BaseModel
import pytest

from custom_components.ttlock.models import EpochMs, PassageModeConfig


class TestEpochMs:
    class model_under_test(BaseModel):
        ts: EpochMs

    @pytest.mark.parametrize(
        ("epoch", "tz", "day", "hour"),
        [
            [1675400802000, "Europe/Amsterdam", 5, 6],
            [1675400802000, "Pacific/Auckland", 5, 18],
            [1675400802000, "America/Los_Angeles", 4, 21],
            [1682244497000, "Europe/Amsterdam", 7, 12],
            [1682244497000, "Pacific/Auckland", 7, 22],
            [1682244497000, "America/Los_Angeles", 7, 3],
        ],
    )
    def test_with_tz(self, hass, epoch, tz, day, hour):
        hass.config.set_time_zone(tz)
        ts = self.model_under_test(ts=epoch).ts
        assert ts.isoweekday() == day
        assert ts.hour == hour


class TestPassageModeConfig:
    def test_passage_mode(self):
        parsed = PassageModeConfig.parse_obj(
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
        parsed = PassageModeConfig.parse_obj(
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
