"""Номера задач И-NNN: последовательность PostgreSQL, номер не переиспользуется."""
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from planerka.settings import get_setting

_SEQ = "task_number_seq"
_LOCK_KEY = 7_204_001  # advisory-замок: два процесса не двигают последовательность одновременно
_PARSE_RE = re.compile(r"^\s*[Ии][\s-]*(\d+)\s*$")


def format_number(n: int) -> str:
    return f"И-{n:03d}"


def parse_number(text_: str) -> int:
    """'И-021', 'и-21', 'И 21' -> 21. Иначе ValueError."""
    m = _PARSE_RE.match(text_)
    if m is None:
        raise ValueError(f"не номер задачи: {text_!r}")
    return int(m.group(1))


def _lock_and_upcoming(session: Session) -> int:
    """Берёт замок и возвращает номер, который выдаст nextval."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    last, is_called = session.execute(
        text(f"SELECT last_value, is_called FROM {_SEQ}")).one()
    return last + 1 if is_called else last


def next_task_number(session: Session) -> int:
    """Следующий номер. Взятый номер не возвращается, даже если кандидата отклонили."""
    start = int(get_setting(session, "registry.task_number_start"))
    if _lock_and_upcoming(session) < start:
        session.execute(text(f"SELECT setval('{_SEQ}', :v, false)"), {"v": start})
    return int(session.execute(text(f"SELECT nextval('{_SEQ}')")).scalar_one())


def set_sequence_after(session: Session, max_number: int) -> None:
    """Следующий выданный номер будет не меньше max_number + 1.

    Последовательность только растёт: повторный импорт не возвращает выданные номера.
    """
    if _lock_and_upcoming(session) <= max_number:
        session.execute(text(f"SELECT setval('{_SEQ}', :v, true)"), {"v": max_number})
