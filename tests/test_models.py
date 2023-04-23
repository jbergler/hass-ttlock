from pydantic import BaseModel
import pytest

from custom_components.ttlock.models import EpochMs


class TestEpochMs:
    class TestModel(BaseModel):
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
        ts = self.TestModel(ts=epoch).ts
        assert ts.isoweekday() == day
        assert ts.hour == hour
