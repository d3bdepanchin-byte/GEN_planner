"""Файлы на уточнение: рассылка исполнителям, приём ответов, напоминания, файл владельцу реестра.

Письма уходят по одному с фиксацией после каждого: сбой на втором не повторяет первое.
"""
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.core.statuses import TaskStatus
from planerka.db.models import Employee, ReviewFile, Source, Task
from planerka.extract.run import EXTRACTED
from planerka.mail.accept import accept_review_mail
from planerka.pipeline.deps import Deps
from planerka.pipeline.handlers.common import alert, ru_date
from planerka.pipeline.transcripts import meeting_dir
from planerka.reminders import company_tz, run_reminders, set_due
from planerka.review.executor import build_executor_file
from planerka.review.owner import build_owner_file, pending_owner_items
from planerka.settings import get_setting

log = logging.getLogger(__name__)

OWNER_ALERTED = 'алерт: владелец реестра не задан'  # флаг источника: алерт уже ушёл


class MailBatchIncomplete(Exception):
    """Часть писем служебного ящика не обработана; они остались во входящих."""


def _in_executor_files(session: Session, numbers: list[int]) -> list[ReviewFile]:
    if not numbers:
        return []
    return list(session.scalars(select(ReviewFile).where(
        ReviewFile.kind == 'executor', ReviewFile.task_numbers.overlap(numbers))))


def send_reviews(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    """Каждому исполнителю — файл с его кандидатами. Без ответственного — ждут владельца реестра."""
    source = session.get(Source, payload['source_id'])
    if source is None or source.status != EXTRACTED:
        log.info('рассылка: источник %s пропущен', payload['source_id'])
        return
    tasks = list(session.scalars(select(Task).where(
        Task.source_id == source.id, Task.status == TaskStatus.CANDIDATE.value,
        Task.owner_id.is_not(None)).order_by(Task.number)))
    sent = {n for rf in _in_executor_files(session, [t.number for t in tasks])
            for n in rf.task_numbers}
    by_owner: dict[int, list[Task]] = defaultdict(list)
    for t in tasks:
        if t.number not in sent:
            by_owner[t.owner_id].append(t)

    workdays = int(get_setting(session, 'review.return_workdays'))
    tz = company_tz(session)
    when = ru_date(source.meeting_date)
    for owner_id, items in by_owner.items():
        employee = session.get(Employee, owner_id)
        if not employee.email:
            alert(session, deps, 'send_reviews', source.id,
                  f'нет e-mail исполнителя, задач {len(items)}')
            continue
        rf = build_executor_file(session, employee, items,
                                 meeting_dir(deps.env, source.id) / 'review')
        rf.sent_at = now
        set_due(rf, deps.cal, workdays, tz)
        body = (f'Во вложении — задачи с планёрки {when}, по которым вы ответственный: '
                f'{len(items)}.\n'
                'В колонке «Решение» выберите для каждой строки: «подтверждаю», «правка» '
                'или «отклоняю».\n'
                f'Верните файл ответом на это письмо до {rf.due_at.astimezone(tz):%d.%m.%Y}.\n')
        rf.message_id = deps.mailer.send(employee.email,
                                         f'Планёрка {when}: задачи на подтверждение', body,
                                         [Path(rf.path)])
        session.commit()
        log.info('источник %s: файл %d отправлен, задач %d', source.id, rf.id, len(items))


def poll_mail(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    """Ответы на файлы. Письмо помечается обработанным только после фиксации в БД."""
    failed = 0
    for mail in deps.inbox.fetch_new():
        try:
            outcome = accept_review_mail(session, mail, deps.mailer,
                                         own_address=deps.env.service_mailbox)
            session.commit()
        except Exception as e:  # noqa: BLE001 — письмо остаётся во входящих до следующего раза
            session.rollback()
            failed += 1
            log.error('письмо не обработано: %s', type(e).__name__)
            continue
        deps.inbox.mark_done(mail.message_id)
        log.info('письмо обработано: %s', outcome.value)
    if failed:
        raise MailBatchIncomplete(f'не обработано писем: {failed}')


def reminders(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    done = run_reminders(session, deps.cal, deps.mailer, now)
    if done:
        log.info('напоминаний и эскалаций: %d', done)


def _source_ready(session: Session, source_id: int) -> bool:
    """Все файлы исполнителей по источнику вернулись или эскалированы (и все разосланы)."""
    source = session.get(Source, source_id)
    if source is None or source.status != EXTRACTED:
        return False
    tasks = list(session.scalars(select(Task).where(Task.source_id == source_id)))
    files = _in_executor_files(session, [t.number for t in tasks])
    in_files = {n for rf in files for n in rf.task_numbers}
    unsent = [t for t in tasks if t.status == TaskStatus.CANDIDATE.value
              and t.owner_id is not None and t.number not in in_files]
    return not unsent and all(rf.returned_at or rf.escalated_at for rf in files)


def _registry_owner(session: Session) -> Employee | None:
    owner_id = int(get_setting(session, 'registry.owner_employee_id'))
    owner = session.get(Employee, owner_id) if owner_id else None
    return owner if owner is not None and owner.email else None


def owner_digest(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    """Источник готов (файлы вернулись или эскалированы) — файл владельцу реестра.

    Тот же набор номеров не отправляется второй раз, пока прошлый файл владельца не вернулся.
    """
    by_source: dict[int, set[int]] = defaultdict(set)
    for task, _ in pending_owner_items(session):
        if task.source_id is not None:
            by_source[task.source_id].add(task.number)
    ready = [sid for sid in sorted(by_source) if _source_ready(session, sid)]
    wanted = {n for sid in ready for n in by_source[sid]}
    covered = {n for rf in session.scalars(select(ReviewFile).where(
        ReviewFile.kind == 'owner', ReviewFile.sent_at.is_not(None),
        ReviewFile.returned_at.is_(None))) for n in rf.task_numbers}
    if not wanted or wanted <= covered:
        return

    owner = _registry_owner(session)
    if owner is None:
        for sid in ready:
            source = session.get(Source, sid)
            if not source.flags.get(OWNER_ALERTED):
                alert(session, deps, 'owner_digest', sid,
                      'не задан владелец реестра или его e-mail')
                source.flags = {**source.flags, OWNER_ALERTED: True}
        return
    rf = build_owner_file(session, owner, deps.env.data_dir / 'registry', now)
    if rf is None:
        return
    rf.sent_at = now
    body = (f'Во вложении — задачи, которые ждут вашего решения: {len(rf.task_numbers)}.\n'
            'В колонке «Решение» выберите для каждой строки: «утверждаю» или «отклоняю».\n'
            'Верните файл ответом на это письмо.\n')
    rf.message_id = deps.mailer.send(owner.email, 'Планёрка: задачи на утверждение', body,
                                     [Path(rf.path)])
    log.info('файл владельцу реестра %d отправлен, задач %d', rf.id, len(rf.task_numbers))
