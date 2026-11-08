from datetime import date, datetime
from pathlib import Path

import pytest

from planerka.core.workdays import Calendar, UnknownYear

REAL_DIR = Path(__file__).resolve().parents[2] / 'config' / 'calendar'


def make_cal(tmp_path):
    (tmp_path / '2026.toml').write_text(
        'holidays = [2026-11-04]' + chr(10) + 'working_weekends = [2026-11-14]')
    return Calendar.load(tmp_path)


def test_weekend_skipped(tmp_path):
    cal = make_cal(tmp_path)
    assert cal.add_workdays(datetime(2026, 11, 6, 10), 1) == datetime(2026, 11, 9, 10)


def test_holiday_skipped(tmp_path):
    cal = make_cal(tmp_path)
    assert cal.add_workdays(datetime(2026, 11, 3, 10), 1) == datetime(2026, 11, 5, 10)


def test_working_weekend_counts(tmp_path):
    assert make_cal(tmp_path).is_workday(date(2026, 11, 14))


def test_unknown_year_raises(tmp_path):
    with pytest.raises(UnknownYear):
        make_cal(tmp_path).is_workday(date(2027, 1, 11))


def test_add_workdays_into_unknown_year_raises(tmp_path):
    with pytest.raises(UnknownYear):
        make_cal(tmp_path).add_workdays(datetime(2026, 12, 30, 9), 5)


def test_real_calendar():
    cal = Calendar.load(REAL_DIR)
    assert not cal.is_workday(date(2026, 1, 9))
    assert not cal.is_workday(date(2026, 12, 31))
    assert cal.is_workday(date(2026, 12, 30))
    assert cal.is_workday(date(2027, 2, 20))
    assert not cal.is_workday(date(2027, 2, 22))
    assert not cal.is_workday(date(2027, 11, 5))
    assert not cal.is_workday(date(2027, 12, 31))
    assert cal.add_workdays(datetime(2026, 12, 30, 9, 30), 1) == datetime(2027, 1, 11, 9, 30)
