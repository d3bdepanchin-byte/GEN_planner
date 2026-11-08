import pytest

from planerka.settings import UnknownSetting, get_setting, set_setting


def test_setting_override_beats_default(session):
    assert get_setting(session, 'review.return_workdays') == 2
    set_setting(session, 'review.return_workdays', 3, 'admin')
    assert get_setting(session, 'review.return_workdays') == 3


def test_set_setting_twice_updates(session):
    set_setting(session, 'review.return_workdays', 3, 'admin')
    set_setting(session, 'review.return_workdays', 4, 'admin2')
    assert get_setting(session, 'review.return_workdays') == 4


def test_list_setting_roundtrip(session):
    set_setting(session, 'goals.goals_2027', ['а', 'б'], 'admin')
    assert get_setting(session, 'goals.goals_2027') == ['а', 'б']


def test_unknown_setting_raises(session):
    with pytest.raises(UnknownSetting):
        get_setting(session, 'nope.nope')


def test_set_unknown_setting_raises(session):
    with pytest.raises(UnknownSetting):
        set_setting(session, 'nope.nope', 1, 'admin')
