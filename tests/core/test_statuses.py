import pytest
from sqlalchemy import select

from planerka.core.statuses import InvalidTransition, TaskStatus, transition
from planerka.db.models import History


def test_rejected_is_final(session, task):
    transition(session, task, TaskStatus.REJECTED, 'owner')
    with pytest.raises(InvalidTransition):
        transition(session, task, TaskStatus.IN_PROGRESS, 'owner')


def test_candidate_cannot_jump_to_done(session, task):
    with pytest.raises(InvalidTransition):
        transition(session, task, TaskStatus.DONE, 'owner')


def test_transition_writes_history_with_timestamp(session, task):
    transition(session, task, TaskStatus.CONFIRMED_BY_EXECUTOR, 'executor')
    h = session.scalars(select(History).where(History.task_id == task.id)).one()
    assert (h.old, h.new) == ('на подтверждении', 'подтверждена исполнителем')
    assert h.actor == 'executor'
    assert h.at is not None


def test_failed_transition_writes_no_history(session, task):
    with pytest.raises(InvalidTransition):
        transition(session, task, TaskStatus.DONE, 'owner')
    assert session.scalars(select(History)).all() == []
    assert task.status == 'на подтверждении'


ALLOWED = [
    (TaskStatus.CANDIDATE, TaskStatus.CONFIRMED_BY_EXECUTOR),
    (TaskStatus.CANDIDATE, TaskStatus.IN_PROGRESS),
    (TaskStatus.CANDIDATE, TaskStatus.REJECTED),
    (TaskStatus.CONFIRMED_BY_EXECUTOR, TaskStatus.IN_PROGRESS),
    (TaskStatus.CONFIRMED_BY_EXECUTOR, TaskStatus.REJECTED),
    (TaskStatus.IN_PROGRESS, TaskStatus.PAUSED),
    (TaskStatus.PAUSED, TaskStatus.IN_PROGRESS),
    (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
    (TaskStatus.PAUSED, TaskStatus.DONE),
    (TaskStatus.DROPPED, TaskStatus.IN_PROGRESS),
    (TaskStatus.DONE, TaskStatus.ARCHIVED),
    (TaskStatus.DONE, TaskStatus.DROPPED),
    (TaskStatus.DROPPED, TaskStatus.ARCHIVED),
]


@pytest.mark.parametrize('old,new', ALLOWED)
def test_allowed_transitions(session, task, old, new):
    task.status = old.value
    transition(session, task, new, 'owner')
    assert task.status == new.value


def test_full_matrix_matches_rules(session, task):
    def allowed(old, new):
        if old == TaskStatus.REJECTED or old == new:
            return False
        if new == TaskStatus.ARCHIVED:
            return True
        if new == TaskStatus.DROPPED:
            return old != TaskStatus.ARCHIVED
        return (old, new) in ALLOWED

    for old in TaskStatus:
        for new in TaskStatus:
            task.status = old.value
            if allowed(old, new):
                transition(session, task, new, 'owner')
                assert task.status == new.value
            else:
                with pytest.raises(InvalidTransition):
                    transition(session, task, new, 'owner')
