"""Teams timestamps parse however many decimals they carry."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prep.config import EstimatorConfig
from prep.estimator import estimate
from prep.isotime import fromiso
from prep.scheduler import parse_dt

TZ = ZoneInfo("Europe/London")


def test_any_number_of_decimals():
    want = datetime(2026, 10, 9, 20, 2, 10, 190000, tzinfo=timezone.utc)
    for text in ("2026-10-09T20:02:10.19Z", "2026-10-09T20:02:10.190Z",
                 "2026-10-09T20:02:10.1900000Z", "2026-10-09T20:02:10.19+00:00"):
        assert fromiso(text) == want
    assert fromiso("2026-10-09T20:02:10Z") == want.replace(microsecond=0)
    assert fromiso("2026-10-09T20:02:10.5Z").microsecond == 500000


def test_a_two_decimal_set_time_still_gets_the_days_it_was_given():
    # ABA Prep 8: set Friday evening, due Thursday. Read as "no set time",
    # it was booked as if set the day before - 30 min instead of 120.
    assigned = parse_dt("2026-10-09T20:02:10.19Z", TZ)
    due = parse_dt("2026-10-15T07:00:00Z", TZ)
    assert assigned is not None
    assert estimate(EstimatorConfig(), "Mr Jones", assigned, due, {}, None).minutes == 120
