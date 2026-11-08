"""Воркер: берёт готовые задания, повторяет с паузой 1, 2, 4… минуты, после лимита — алерт.

Каждое задание — своя транзакция: захват (running) фиксируется, затем обработчик, затем итог.
MaskingUncertain обработчик разбора гасит сам (повтор не поможет). Любая другая ошибка —
повтор до jobs.max_attempts, затем «failed» и алерт разработчику (только тип ошибки).
"""
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from planerka.db.models import Job
from planerka.pipeline.deps import Deps, build_deps, build_masker
from planerka.pipeline.handlers import HANDLERS
from planerka.pipeline.handlers.common import alert
from planerka.pipeline.jobs import (DONE, FAILED, PENDING, RUNNING, claim_next, ensure_periodic,
                                    recover_running)
from planerka.settings import Env, get_setting, load_defaults

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    done: int = 0
    retried: int = 0
    failed: int = 0

    @property
    def ran(self) -> int:
        return self.done + self.retried + self.failed


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


def _retry_or_fail(session: Session, deps: Deps, job: Job, error: Exception,
                   now: datetime) -> bool:
    """True — задание снова в очереди; False — попытки кончились, алерт отправлен."""
    job.attempts += 1
    job.last_error = type(error).__name__
    source_id = job.payload.get('source_id')
    if job.attempts < int(get_setting(session, 'jobs.max_attempts')):
        job.status = PENDING
        job.run_after = now + timedelta(minutes=2 ** (job.attempts - 1))
        log.warning('задание %s (%s, источник %s): %s, попытка %d, повтор %s', job.id, job.kind,
                    source_id, job.last_error, job.attempts, job.run_after.isoformat())
        return True
    job.status = FAILED
    log.error('задание %s (%s, источник %s): %s, попыток %d — остановлено', job.id, job.kind,
              source_id, job.last_error, job.attempts)
    alert(session, deps, job.kind, source_id, f'{job.last_error}, попыток {job.attempts}')
    return False


def _sleep_seconds(session: Session, now: datetime) -> float:
    """До ближайшего задания, но не дольше intake.poll_interval_sec и не меньше секунды."""
    cap = float(get_setting(session, 'intake.poll_interval_sec'))
    nxt = session.scalar(select(func.min(Job.run_after)).where(Job.status == PENDING))
    wait = cap if nxt is None else (nxt - now).total_seconds()
    return min(max(wait, 1.0), cap)


def run_worker(env: Env) -> None:
    """Бесконечный цикл до SIGTERM/SIGINT: периодические задания, готовые задания, пауза."""
    stop = False

    def _stop(signum, frame) -> None:
        nonlocal stop
        stop = True
        log.info('воркер: получен сигнал %d, завершаю после текущего задания', signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    engine = create_engine(env.database_url, pool_pre_ping=True)
    with Session(engine) as session:
        log.info('воркер: прерванных заданий возвращено в очередь %d', recover_running(session))
        deps = build_deps(session, env)
        session.commit()
        while not stop:
            now = datetime.now(timezone.utc)
            try:
                # Справочник и допуски маскирования — свежие на каждый проход.
                deps.masker = build_masker(session, deps.tokens, deps.names)
                ensure_periodic(session, now)
                session.commit()
                report = run_once(session, deps, now)
                if report.ran:
                    log.info('воркер: выполнено %d, повтор %d, остановлено %d', report.done,
                             report.retried, report.failed)
                pause = _sleep_seconds(session, datetime.now(timezone.utc))
                session.commit()
            except Exception as e:  # noqa: BLE001 — БД недоступна и т.п.: подождать и снова
                session.rollback()
                log.error('воркер: сбой цикла: %s', type(e).__name__)
                pause = float(load_defaults()['intake.poll_interval_sec'])  # без обращения к БД
            _interruptible_sleep(pause, lambda: stop)
    engine.dispose()


def _interruptible_sleep(seconds: float, stopped) -> None:
    end = time.monotonic() + seconds
    while not stopped() and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))
