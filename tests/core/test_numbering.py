import pytest

from planerka.core.numbering import (
    format_number, next_task_number, parse_number, set_sequence_after)
from planerka.settings import set_setting


def test_format_pads_to_three_digits():
    assert format_number(7) == 'И-007'
    assert format_number(1234) == 'И-1234'


def test_parse_accepts_spoken_variants():
    assert parse_number('И-021') == 21
    assert parse_number('и-21') == 21
    assert parse_number('И 21') == 21


@pytest.mark.parametrize('text', ['задача двадцать один', 'И-', '21', 'И-2a', 'А-21', ''])
def test_parse_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_number(text)


def test_numbers_never_reused(session):
    a = next_task_number(session)
    assert next_task_number(session) == a + 1


def test_rejected_candidate_number_not_returned(session, task):
    n = task.number
    session.delete(task)
    session.flush()
    assert next_task_number(session) > n


def test_sequence_continues_after_pilot(session):
    set_sequence_after(session, 72)
    assert next_task_number(session) == 73


def test_start_setting_moves_sequence_up(session):
    set_sequence_after(session, 0)
    set_setting(session, 'registry.task_number_start', 500, 'test')
    assert next_task_number(session) == 500
    assert next_task_number(session) == 501


def test_start_setting_never_moves_sequence_back(session):
    set_sequence_after(session, 900)
    set_setting(session, 'registry.task_number_start', 500, 'test')
    assert next_task_number(session) == 901
