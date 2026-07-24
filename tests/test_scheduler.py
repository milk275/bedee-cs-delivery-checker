from datetime import datetime
from zoneinfo import ZoneInfo

from klean_pod_checker.scheduler import next_run


ZONE = ZoneInfo("Asia/Bangkok")


def test_next_run_uses_next_hour_inside_window():
    now = datetime(2026, 7, 24, 9, 15, tzinfo=ZONE)
    assert next_run(now) == datetime(2026, 7, 24, 10, 0, tzinfo=ZONE)


def test_next_run_keeps_exact_end_hour():
    now = datetime(2026, 7, 24, 21, 30, tzinfo=ZONE)
    assert next_run(now) == datetime(2026, 7, 24, 22, 0, tzinfo=ZONE)


def test_next_run_moves_to_tomorrow_after_window():
    now = datetime(2026, 7, 24, 22, 1, tzinfo=ZONE)
    assert next_run(now) == datetime(2026, 7, 25, 8, 0, tzinfo=ZONE)
