"""Статусы задачи и разрешённые переходы."""
from enum import Enum

from sqlalchemy.orm import Session

from planerka.db.models import History, Task


class TaskStatus(str, Enum):
    CANDIDATE = "на подтверждении"
    CONFIRMED_BY_EXECUTOR = "подтверждена исполнителем"
    IN_PROGRESS = "в работе"
    PAUSED = "на паузе"
    DONE = "выполнена"
    DROPPED = "снята с повестки"
    ARCHIVED = "архив"
    REJECTED = "отклонена"


S = TaskStatus

TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.CANDIDATE: frozenset({S.CONFIRMED_BY_EXECUTOR, S.IN_PROGRESS, S.REJECTED, S.DROPPED, S.ARCHIVED}),
    S.CONFIRMED_BY_EXECUTOR: frozenset({S.IN_PROGRESS, S.REJECTED, S.DROPPED, S.ARCHIVED}),
    S.IN_PROGRESS: frozenset({S.PAUSED, S.DONE, S.DROPPED, S.ARCHIVED}),
    S.PAUSED: frozenset({S.IN_PROGRESS, S.DONE, S.DROPPED, S.ARCHIVED}),
    S.DONE: frozenset({S.DROPPED, S.ARCHIVED}),
    S.DROPPED: frozenset({S.IN_PROGRESS, S.ARCHIVED}),
    S.ARCHIVED: frozenset(),
    S.REJECTED: frozenset(),
}


class InvalidTransition(Exception):
    pass


def transition(session: Session, task: Task, new: TaskStatus, actor: str) -> None:
    old = TaskStatus(task.status)
    if new not in TRANSITIONS[old]:
        raise InvalidTransition(f"{old.value} -> {new.value}")
    task.status = new.value
    session.add(History(task_id=task.id, event="status", old=old.value,
                        new=new.value, actor=actor))
    session.flush()
