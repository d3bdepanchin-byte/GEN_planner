"""Воркер: готовые задания, повтор с паузой 1, 2, 4… минуты (не дольше jobs.max_pause_min).

Каждое задание — своя транзакция: захват (running) фиксируется, затем обработчик, затем итог.
Временная недоступность (распознаватель, LLM API) источник не останавливает: задание остаётся
в очереди, один алерт, когда попыток стало jobs.max_attempts. Прочие ошибки — повтор
до jobs.max_attempts, затем «failed» и алерт с командой повтора. MaskingUncertain обработчик
разбора гасит сам. Воркер один (журнал решений): прерванные задания возвращает он сам.
"""
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from planerka.asr.base import TranscriberUnavailable, TranscriptionFailed
from planerka.db.models import Job
from planerka.llm.base import LLMUnavailable
from planerka.pipeline.deps import Deps, build_deps, build_masker
from planerka.pipeline.handlers import HANDLERS
from planerka.pipeline.handlers.common import alert, requeue_hint
from planerka.pipeline.jobs import (DONE, FAILED, PENDING, PERIODIC, RUNNING, claim_next,
                                    ensure_periodic)
from planerka.settings import Env, get_setting, load_defaults

log = logging.getLogger(__name__)


class JobInterrupted(Exception):
    """Задание осталось «running»: воркер упал или потерял БД посреди задания."""


@dataclass
class RunReport:
    done: int = 0
    retried: int = 0
    failed: int = 0

    @property
    def ran(self) -> int:
        return self.done + self.retried + self.failed


@dataclass
class WorkerState:
    needs_recovery: bool = True  # при старте и после сбоя цикла


def run_once(session: Session, deps: Deps, now: datetime) -> RunReport:
    """Все задания, готовые к `now`, включая поставленные по ходу (распознавание → разбор)."""
    report = RunReport()
    while (job := claim_next(session, now)) is not None:
        job.status = RUNNING
        session.commit()
        try:
            HANDLERS[job.kind](session, deps, dict(job.payload), now)
            job.status = DONE
            job.last_error = None
            session.commit()
            report.done += 1
        except Exception as e:  # noqa: BLE001 — любая ошибка обработчика: повтор или алерт
            session.rollback()
            if _retry_or_fail(session, deps, job, e, now):
                report.retried += 1
            else:
                report.failed += 1
            session.commit()
    return report


def _temporary(error: Exception) -> bool:
    """Сервис недоступен — подождать; битый файл (TranscriptionFailed) — не временная ошибка."""
    return (isinstance(error, (TranscriberUnavailable, LLMUnavailable))
            and not isinstance(error, TranscriptionFailed))


def _retry_or_fail(session: Session, deps: Deps, job: Job, error: Exception,
                   now: datetime) -> bool:
    """True — задание снова в очереди; False — «failed» (алерт, если это новая ошибка)."""
    job.attempts += 1
    job.last_error = type(error).__name__
    source_id = job.payload.get('source_id')
    limit = int(get_setting(session, 'jobs.max_attempts'))
    pause = min(2 ** min(job.attempts - 1, 30), int(get_setting(session, 'jobs.max_pause_min')))
    if _temporary(error) or job.attempts < limit:
        job.status = PENDING
        job.run_after = now + timedelta(minutes=pause)
        log.warning('задание %s (%s, источник %s): %s, попытка %d, повтор %s', job.id, job.kind,
                    source_id, job.last_error, job.attempts, job.run_after.isoformat())
        if _temporary(error) and job.attempts == limit:
            alert(session, deps, job.kind, source_id,
                  f'{job.last_error}, попыток {job.attempts}; задание в очереди, '
                  'повтор автоматически')
        return True
    job.status = FAILED
    log.error('задание %s (%s, источник %s): %s, попыток %d — остановлено', job.id, job.kind,
              source_id, job.last_error, job.attempts)
    if not _same_failure_as_before(session, job):
        hint = f'; {requeue_hint(source_id)}' if source_id is not None else ''
        alert(session, deps, job.kind, source_id,
              f'{job.last_error}, попыток {job.attempts}{hint}')
    return False


def _same_failure_as_before(session: Session, job: Job) -> bool:
    """Периодическое задание упало так же, как предыдущее того же вида: алерт уже был."""
    if job.kind not in PERIODIC:
        return False
    prev = session.scalars(select(Job).where(Job.kind == job.kind, Job.id < job.id)
                           .order_by(Job.id.desc()).limit(1)).first()
    return prev is not None and prev.status == FAILED and prev.last_error == job.last_error


def recover_interrupted(session: Session, deps: Deps, now: datetime) -> int:
    """Задания «running» — прерванная попытка: считается, как ошибка. Число заданий."""
    jobs = list(session.scalars(select(Job).where(Job.status == RUNNING).order_by(Job.id)))
    for job in jobs:
        _retry_or_fail(session, deps, job, JobInterrupted(), now)
    if jobs:
        log.warning('воркер: прерванных заданий %d', len(jobs))
    return len(jobs)


def _sleep_seconds(session: Session, now: datetime) -> float:
    """До ближайшего задания, но не дольше intake.poll_interval_sec и не меньше секунды."""
    cap = float(get_setting(session, 'intake.poll_interval_sec'))
    nxt = session.scalar(select(func.min(Job.run_after)).where(Job.status == PENDING))
    wait = cap if nxt is None else (nxt - now).total_seconds()
    return min(max(wait, 1.0), cap)


def worker_cycle(session: Session, deps: Deps, state: WorkerState, now: datetime,
                 prepare: Callable[[], None] | None = None) -> float:
    """Один проход воркера; возвращает паузу в секундах. Сбой (БД и т.п.) не роняет цикл:
    на следующем удачном проходе задания, оставшиеся «running», возвращаются в очередь."""
    try:
        if state.needs_recovery:
            recover_interrupted(session, deps, now)
            session.commit()
            state.needs_recovery = False
        if prepare is not None:
            prepare()
        ensure_periodic(session, now)
        session.commit()
        report = run_once(session, deps, now)
        if report.ran:
            log.info('воркер: выполнено %d, повтор %d, остановлено %d', report.done,
                     report.retried, report.failed)
        pause = _sleep_seconds(session, datetime.now(timezone.utc))
        session.commit()
        return pause
    except Exception as e:  # noqa: BLE001 — БД недоступна и т.п.: подождать и снова
        session.rollback()
        state.needs_recovery = True
        log.error('воркер: сбой цикла: %s', type(e).__name__)
        return float(load_defaults()['intake.poll_interval_sec'])  # без обращения к БД


def run_worker(env: Env) -> None:
    """Бесконечный цикл до SIGTERM/SIGINT: проход воркера, пауза."""
    stop = False

    def _stop(signum, frame) -> None:
        nonlocal stop
        stop = True
        log.info('воркер: получен сигнал %d, завершаю после текущего задания', signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    engine = create_engine(env.database_url, pool_pre_ping=True)
    with Session(engine) as session:
        deps = build_deps(session, env)
        session.commit()
        state = WorkerState()

        def refresh() -> None:  # справочник и допуски маскирования — свежие на каждый проход
            deps.masker = build_masker(session, deps.tokens, deps.names)

        while not stop:
            pause = worker_cycle(session, deps, state, datetime.now(timezone.utc), refresh)
            _interruptible_sleep(pause, lambda: stop)
    engine.dispose()


def _interruptible_sleep(seconds: float, stopped) -> None:
    end = time.monotonic() + seconds
    while not stopped() and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))
