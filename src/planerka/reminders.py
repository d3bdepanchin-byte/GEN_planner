"""Сроки возврата файлов, напоминания исполнителю, эскалация владельцу реестра (Д-44, Д-45)."""
from dataclasses import dataclass
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.core.directory import load_directory
from planerka.core.numbering import format_number
from planerka.core.statuses import TaskStatus
from planerka.core.workdays import Calendar
from planerka.db.models import Employee, ReviewFile, Task
from planerka.mail.base import Mailer
from planerka.notify import send_alert_safe
from planerka.review.executor import _add_unique
from planerka.settings import get_setting

log = logging.getLogger(__name__)
FLAG_NEXT_MEETING = "на ближайшую планёрку"
ALERT_KIND = "напоминание"


class ReminderKind(Enum):
    REMIND_EXECUTOR = "напомнить исполнителю"
    ESCALATE_OWNER = "эскалировать владельцу"


@dataclass(frozen=True)
class ReminderAction:
    kind: ReminderKind
    review_id: int


def company_tz(session: Session) -> ZoneInfo:
    return ZoneInfo(get_setting(session, "company.timezone"))


def _add_local_workdays(cal: Calendar, start: datetime, n: int,
                        tz: ZoneInfo | None) -> datetime:
    """Рабочие дни считаются по местной дате компании; результат — в UTC."""
    tz = tz or timezone.utc
    return cal.add_workdays(start.astimezone(tz), n).astimezone(timezone.utc)


def set_due(review: ReviewFile, cal: Calendar, workdays: int,
            tz: ZoneInfo | None = None) -> None:
    """«Без реакции» отсчитывается от отправки файла (Д-45). tz — company_tz(session)."""
    review.due_at = _add_local_workdays(cal, review.sent_at, workdays, tz)


def due_actions(session: Session, cal: Calendar, now: datetime) -> list[ReminderAction]:
    escalate_after = get_setting(session, "review.escalate_after_workdays")
    tz = company_tz(session)
    files = session.scalars(select(ReviewFile).where(
        ReviewFile.kind == "executor", ReviewFile.sent_at.is_not(None),
        ReviewFile.due_at.is_not(None), ReviewFile.returned_at.is_(None),
    ).order_by(ReviewFile.id))
    actions: list[ReminderAction] = []
    for rf in files:
        if rf.reminded_at is None and now >= rf.due_at:
            actions.append(ReminderAction(ReminderKind.REMIND_EXECUTOR, rf.id))
        if rf.escalated_at is None and now >= _add_local_workdays(cal, rf.due_at, escalate_after, tz):
            actions.append(ReminderAction(ReminderKind.ESCALATE_OWNER, rf.id))
    return actions


def run_reminders(session: Session, cal: Calendar, mailer: Mailer, now: datetime) -> int:
    """Число выполненных действий. Ошибка почты по одному файлу не мешает остальным."""
    done = 0
    for action in due_actions(session, cal, now):
        rf = session.get(ReviewFile, action.review_id)
        remind = action.kind is ReminderKind.REMIND_EXECUTOR
        try:
            with session.begin_nested():
                ok = (_remind if remind else _escalate)(session, mailer, rf)
                if ok is not None:  # обработано: письмо или алерт разработчику отправлены
                    if remind:
                        rf.reminded_at = now
                    else:
                        rf.escalated_at = now
        except Exception as e:  # noqa: BLE001 — одна ошибка не должна останавливать цикл
            log.error("напоминание по файлу %s (%s): %s", rf.id, action.kind.name,
                      type(e).__name__)
            continue
        done += bool(ok)
    return done


def _numbers(rf: ReviewFile) -> str:
    return ", ".join(format_number(n) for n in dict.fromkeys(rf.task_numbers))


def _alert_missing_email(session: Session, mailer: Mailer, rf: ReviewFile,
                         who: str) -> bool | None:
    """False — алерт отправлен (действие обработано); None — алерт не ушёл, повторить."""
    developer = get_setting(session, "alerts.developer_email")
    sent = send_alert_safe(mailer, [developer] if developer else [], ALERT_KIND, rf.id,
                           f"нет e-mail: {who}", load_directory(session))
    return False if sent else None


def _remind(session: Session, mailer: Mailer, rf: ReviewFile) -> bool | None:
    executor = session.get(Employee, rf.employee_id) if rf.employee_id else None
    if executor is None or not executor.email:
        return _alert_missing_email(session, mailer, rf, "исполнитель файла")
    body = (f"Файл с задачами на уточнение ещё не вернулся: {_numbers(rf)}.\n"
            "Проверьте строки, заполните колонку «Решение» и пришлите файл ответом на это письмо.\n"
            "Файл во вложении.\n")
    path = Path(rf.path)
    mailer.send(executor.email, "Файл на уточнение: напоминание", body,
                [path] if path.is_file() else [])
    return True


def _escalate(session: Session, mailer: Mailer, rf: ReviewFile) -> bool | None:
    owner_id = get_setting(session, "registry.owner_employee_id")
    owner = session.get(Employee, owner_id)
    if owner is None or not owner.email:
        return _alert_missing_email(session, mailer, rf, "владелец реестра")
    executor = session.get(Employee, rf.employee_id) if rf.employee_id else None
    name = executor.full_name if executor else "исполнитель не указан"
    tasks = session.scalars(select(Task).where(
        Task.number.in_(rf.task_numbers), Task.status == TaskStatus.CANDIDATE.value,
    ).order_by(Task.number))
    for t in tasks:
        _add_unique(t.flags, FLAG_NEXT_MEETING)
    body = (f"{name} не вернул файл на уточнение: {_numbers(rf)}.\n"
            f"Задачи без ответа помечены «{FLAG_NEXT_MEETING}».\n")
    mailer.send(owner.email, "Файл на уточнение: нет ответа", body, [])
    return True
