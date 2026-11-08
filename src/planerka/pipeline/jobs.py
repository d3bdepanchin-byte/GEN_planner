"""Очередь заданий в таблице job (без брокера): постановка, периодические задания, захват.

Статусы задания: pending → running → done | failed; при ошибке — снова pending с паузой.
Периодические задания ставит сам воркер, если такого же нет в очереди (R24).
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planerka.db.models import Job
from planerka.settings import get_setting

PENDING, RUNNING, DONE, FAILED = 'pending', 'running', 'done', 'failed'

SCAN_INBOX, TRANSCRIBE, EXTRACT = 'scan_inbox', 'transcribe', 'extract'
SEND_REVIEWS, POLL_MAIL = 'send_reviews', 'poll_mail'
REMINDERS, OWNER_DIGEST = 'reminders', 'owner_digest'
KINDS = (SCAN_INBOX, TRANSCRIBE, EXTRACT, SEND_REVIEWS, POLL_MAIL, REMINDERS, OWNER_DIGEST)

# Вид периодического задания → настройка с интервалом в секундах.
PERIODIC = {
    SCAN_INBOX: 'intake.poll_interval_sec',
    POLL_MAIL: 'mail.poll_interval_sec',
    REMINDERS: 'reminders.interval_sec',
    OWNER_DIGEST: 'reminders.interval_sec',
}


def enqueue(session: Session, kind: str, payload: dict,
            run_after: datetime | None = None) -> Job:
    """Новое задание. run_after None — сейчас по часам БД."""
    if kind not in KINDS:
        raise ValueError(f'неизвестный вид задания: {kind}')
    job = Job(kind=kind, payload=dict(payload), status=PENDING, attempts=0)
    if run_after is not None:
        job.run_after = run_after
    session.add(job)
    session.flush()
    return job


def ensure_periodic(session: Session, now: datetime) -> list[Job]:
    """Ставит периодические задания, которых нет в очереди. Следующее — через интервал
    после предыдущего; первое — сейчас."""
    created = []
    for kind, setting in PERIODIC.items():
        queued = session.scalar(select(func.count()).select_from(Job).where(
            Job.kind == kind, Job.status.in_((PENDING, RUNNING))))
        if queued:
            continue
        last = session.scalar(select(func.max(Job.run_after)).where(Job.kind == kind))
        interval = timedelta(seconds=int(get_setting(session, setting)))
        created.append(enqueue(session, kind, {}, now if last is None else last + interval))
    return created


def claim_next(session: Session, now: datetime) -> Job | None:
    """Готовое задание под замком строки (FOR UPDATE SKIP LOCKED): второй воркер его не возьмёт."""
    return session.scalars(
        select(Job).where(Job.status == PENDING, Job.run_after <= now)
        .order_by(Job.run_after, Job.id).limit(1).with_for_update(skip_locked=True)).first()
