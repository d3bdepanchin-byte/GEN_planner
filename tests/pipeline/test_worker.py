import smtplib
import zipfile
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from planerka.db.models import Job, ReviewFile, Source
from planerka.llm.base import JsonLLMBase, LLMUnavailable
from planerka.mail.base import IncomingMail, MailError
from planerka.mail.fake import FakeInbox
from planerka.pipeline import worker
from planerka.pipeline.handlers import review as review_handlers
from planerka.pipeline.jobs import enqueue, ensure_periodic
from planerka.pipeline.worker import WorkerState, recover_interrupted, run_once, worker_cycle
from planerka.settings import set_setting
from samples import (DEV, EXPECTED_CANDIDATES, INBOX_NAME, PLANERKA, TWO_CANDIDATES,
                     BrokenFileTranscriber, tasks_of)


def test_masking_uncertain_never_calls_llm(session, deps_leaky_text, source_ready, run_until_idle):
    run_until_idle(session, deps_leaky_text)
    assert deps_leaky_text.llm.requests == []
    assert session.get(Source, source_ready.id).status == 'маскирование не уверено'
    assert deps_leaky_text.mailer.sent
    assert deps_leaky_text.mailer.sent[-1]['to'] == DEV
    assert 'Смирнов' not in deps_leaky_text.mailer.sent[-1]['body']
    assert f'planerka requeue {source_ready.id}' in deps_leaky_text.mailer.sent[-1]['body']


def test_one_on_one_deferred_in_1a(session, deps_one_on_one, source_ready, run_until_idle):
    run_until_idle(session, deps_one_on_one)
    source = session.get(Source, source_ready.id)
    assert source.status == 'отложено до 1б'
    assert source.kind == 'встреча 1:1'
    assert deps_one_on_one.llm.requests == []


def test_not_a_meeting_asks_host(session, deps_no_phrase, source_ready, host, run_until_idle):
    run_until_idle(session, deps_no_phrase)
    assert session.get(Source, source_ready.id).status == 'не встреча'
    assert deps_no_phrase.mailer.sent[-1]['to'] == host.email
    alerts = [m for m in deps_no_phrase.mailer.sent if m['to'] == DEV]
    assert len(alerts) == 1
    assert f'planerka requeue {source_ready.id} --as-planerka' in alerts[0]['body']


def test_not_a_meeting_mail_text(session, deps_no_phrase, source_ready, host, run_until_idle):
    run_until_idle(session, deps_no_phrase)
    mail = deps_no_phrase.mailer.sent[-1]
    body = mail['body']
    assert '11.11.2026' in mail['subject'] and f'«{INBOX_NAME}»' in body and '11.11.2026' in body
    assert f'Если это планёрка — перешлите это письмо на {DEV}.' in body
    assert ('Чтобы запись разобралась автоматически, планёрку начинают с фразы '
            '«Начинаем планёрку».') in body
    assert 'На это письмо не отвечайте: ящик читает только система.' in body
    assert 'источник' not in body.lower() and 'Следующую' not in body


def test_host_mail_without_developer_email(session, deps_no_phrase, source_ready, host,
                                           run_until_idle):
    set_setting(session, 'alerts.developer_email', '', 'test')
    run_until_idle(session, deps_no_phrase)
    assert 'перешлите' not in deps_no_phrase.mailer.sent[-1]['body']


def test_no_speech_mail_text(session, deps_silence, source_ready, host, run_until_idle):
    run_until_idle(session, deps_silence)
    body = deps_silence.mailer.sent[-1]['body']
    assert f'«{INBOX_NAME}»' in body
    assert 'Если файл не тот — положите в папку правильную запись.' in body
    assert 'На это письмо не отвечайте: ящик читает только система.' in body
    assert 'ещё раз' not in body
    assert 'перешлите' not in body  # requeue «нет речи» не повторяет: пересылать незачем


def test_no_speech_marks_source_and_mails_host(session, deps_silence, source_ready, host,
                                               run_until_idle):
    run_until_idle(session, deps_silence)
    assert session.get(Source, source_ready.id).status == 'нет речи'
    assert [m['to'] for m in deps_silence.mailer.sent] == [host.email]


def test_host_mail_retried_without_new_transcription(session, deps_no_phrase, source_ready,
                                                     host, clock, monkeypatch):
    calls = []
    real = deps_no_phrase.transcriber.transcribe
    monkeypatch.setattr(deps_no_phrase.transcriber, 'transcribe',
                        lambda wav: calls.append(wav) or real(wav))
    sent = deps_no_phrase.mailer.send

    def down(*a, **kw):
        raise OSError('smtp down')
    monkeypatch.setattr(deps_no_phrase.mailer, 'send', down)
    run_once(session, deps_no_phrase, clock.now)
    monkeypatch.setattr(deps_no_phrase.mailer, 'send', sent)
    run_once(session, deps_no_phrase, clock.now + timedelta(minutes=1))
    assert len(calls) == 1
    assert [m['to'] for m in deps_no_phrase.mailer.sent] == [host.email]


def _run_attempts(session, deps, clock, kind, n):
    """n запусков задания: часы каждый раз переводятся к его run_after."""
    pauses = []
    for _ in range(n):
        run_once(session, deps, clock.now)
        job = session.scalars(select(Job).where(Job.kind == kind)).one()
        pauses.append(job.run_after - clock.now)
        clock.now = job.run_after
    return job, pauses


def test_transcriber_down_keeps_source_queued(session, deps_asr_down, source_ready, clock):
    set_setting(session, 'jobs.max_pause_min', 4, 'test')
    job, pauses = _run_attempts(session, deps_asr_down, clock, 'transcribe', 8)
    assert (job.attempts, job.status) == (8, 'pending')
    assert pauses == [timedelta(minutes=m) for m in (1, 2, 4, 4, 4, 4, 4, 4)]
    alerts = [m for m in deps_asr_down.mailer.sent if m['to'] == DEV]
    assert len(alerts) == 1  # ровно один, когда попыток стало jobs.max_attempts
    assert 'TranscriberUnavailable' in alerts[0]['body']
    assert str(source_ready.id) in alerts[0]['body']
    assert session.get(Source, source_ready.id).status == 'получен'


def test_broken_file_stops_with_alert(session, make_deps, source_ready, run_until_idle):
    deps = make_deps(None)
    deps.transcriber = BrokenFileTranscriber()
    run_until_idle(session, deps)
    job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
    assert (job.attempts, job.status) == (5, 'failed')
    alert = deps.mailer.sent[-1]
    assert alert['to'] == DEV and 'TranscriptionFailed' in alert['body']
    assert f'planerka requeue {source_ready.id}' in alert['body']


def test_retry_pause_doubles(session, deps_asr_down, source_ready, clock):
    t0 = clock.now
    pauses = []
    for _ in range(4):
        run_once(session, deps_asr_down, clock.now)
        job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
        pauses.append(job.run_after - clock.now)
        clock.now = job.run_after
    assert pauses == [timedelta(minutes=m) for m in (1, 2, 4, 8)]
    assert clock.now - t0 == timedelta(minutes=15)


def test_max_attempts_is_a_setting(session, make_deps, source_ready, run_until_idle):
    set_setting(session, 'jobs.max_attempts', 2, 'test')
    deps = make_deps(None)
    deps.transcriber = BrokenFileTranscriber()
    run_until_idle(session, deps)
    job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
    assert (job.attempts, job.status) == (2, 'failed')


class DownLLM(JsonLLMBase):
    def __init__(self):
        super().__init__(3)
        self.requests: list[str] = []

    def _send(self, system: str, user: str) -> str:
        self.requests.append(user)
        raise LLMUnavailable('LLM: статус 529')


def test_llm_unavailable_keeps_source_queued(session, make_deps, source_ready, clock):
    deps = make_deps(PLANERKA, llm=DownLLM())
    run_once(session, deps, clock.now)  # распознавание → разбор → LLM недоступен
    clock.now = session.scalars(select(Job).where(Job.kind == 'extract')).one().run_after
    job, _ = _run_attempts(session, deps, clock, 'extract', 6)
    assert (job.attempts, job.status) == (7, 'pending')
    assert session.get(Source, source_ready.id).status == 'распознан'
    assert tasks_of(session, source_ready) == []
    alerts = [m for m in deps.mailer.sent if m['to'] == DEV]
    assert len(alerts) == 1 and 'LLMUnavailable' in alerts[0]['body']


def test_duplicate_job_is_idempotent(session, deps, source_ready, run_until_idle):
    enqueue(session, 'extract', {'source_id': source_ready.id})
    enqueue(session, 'extract', {'source_id': source_ready.id})
    run_until_idle(session, deps)
    assert len(tasks_of(session, source_ready)) == EXPECTED_CANDIDATES


def test_planerka_flow_sends_executor_file(session, deps, source_ready, executor, env,
                                           run_until_idle):
    run_until_idle(session, deps)
    source = session.get(Source, source_ready.id)
    assert (source.kind, source.status) == ('планёрка', 'разобран')
    transcript = env.data_dir / 'meetings' / str(source.id) / 'transcript.json'
    assert transcript.is_file()
    mails = [m for m in deps.mailer.sent if m['to'] == executor.email]
    assert len(mails) == 1
    assert mails[0]['subject'] == 'Планёрка 11.11.2026: задачи на подтверждение'
    assert Path(mails[0]['attachments'][0]).is_file()
    rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).one()
    owned = [t for t in tasks_of(session, source) if t.owner_id == executor.id]
    assert rf.task_numbers == [t.number for t in owned]
    assert rf.message_id == mails[0]['message_id']
    assert rf.sent_at is not None and rf.due_at > rf.sent_at
    local_due = rf.due_at.astimezone(ZoneInfo('Asia/Barnaul')).strftime('%d.%m.%Y')
    assert local_due in mails[0]['body']


def test_send_reviews_twice_sends_once(session, deps, processed_source, executor, clock):
    enqueue(session, 'send_reviews', {'source_id': processed_source.id})
    enqueue(session, 'send_reviews', {'source_id': processed_source.id})
    run_once(session, deps, clock.now)
    assert len([m for m in deps.mailer.sent if m['to'] == executor.email]) == 1


def test_executor_mail_failure_retried(session, make_deps, source_ready, executor,
                                       run_until_idle, monkeypatch):
    deps = make_deps(PLANERKA, TWO_CANDIDATES)
    real = deps.mailer.send
    fails = iter([True])

    def flaky(to, *a, **kw):
        if to == executor.email and next(fails, False):
            raise OSError('smtp down')
        return real(to, *a, **kw)
    monkeypatch.setattr(deps.mailer, 'send', flaky)
    run_until_idle(session, deps)
    assert len([m for m in deps.mailer.sent if m['to'] == executor.email]) == 1
    rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).one()
    assert rf.sent_at is not None


def _owner_mails(deps, owner):
    return [m for m in deps.mailer.sent if m['to'] == owner.email]


def test_owner_digest_waits_for_executor_files(session, deps, processed_source, registry_owner,
                                               clock):
    enqueue(session, 'owner_digest', {})
    run_once(session, deps, clock.now)
    assert _owner_mails(deps, registry_owner) == []  # файл исполнителя ещё не вернулся

    rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).one()
    rf.returned_at = clock.now
    enqueue(session, 'owner_digest', {})
    run_once(session, deps, clock.now)
    mails = _owner_mails(deps, registry_owner)
    assert len(mails) == 1 and Path(mails[0]['attachments'][0]).is_file()
    owner_rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'owner')).one()
    no_owner = [t.number for t in tasks_of(session, processed_source) if t.owner_id is None]
    assert set(no_owner) <= set(owner_rf.task_numbers)
    assert owner_rf.sent_at is not None and owner_rf.message_id == mails[0]['message_id']

    enqueue(session, 'owner_digest', {})
    run_once(session, deps, clock.now)
    assert len(_owner_mails(deps, registry_owner)) == 1  # тот же набор номеров — не повторяем


def test_owner_digest_after_escalation(session, deps, processed_source, registry_owner, clock):
    rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).one()
    rf.escalated_at = clock.now
    enqueue(session, 'owner_digest', {})
    run_once(session, deps, clock.now)
    owner_rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'owner')).one()
    assert set(rf.task_numbers) <= set(owner_rf.task_numbers)


def test_owner_digest_without_owner_alerts_once(session, deps, processed_source, clock):
    set_setting(session, 'registry.owner_employee_id', 0, 'test')
    rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).one()
    rf.returned_at = clock.now
    before = len(deps.mailer.sent)
    for _ in range(2):
        enqueue(session, 'owner_digest', {})
        run_once(session, deps, clock.now)
    alerts = deps.mailer.sent[before:]
    assert [m['to'] for m in alerts] == [DEV]
    assert session.scalars(select(ReviewFile).where(ReviewFile.kind == 'owner')).all() == []


def test_poll_mail_marks_done_after_accept(session, deps, settings, clock):
    deps.inbox = FakeInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл')])
    enqueue(session, 'poll_mail', {})
    run_once(session, deps, clock.now)
    assert deps.inbox.done == ['<m1@example.test>']
    assert deps.mailer.sent[-1]['in_reply_to'] == '<m1@example.test>'  # подсказка: нет .xlsx


def test_poll_mail_failure_not_marked(session, deps, settings, clock, monkeypatch):
    deps.inbox = FakeInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл'),
                            IncomingMail('<m2@example.test>', 'b@example.test', 'Re: файл')])
    _refuse_for(deps, monkeypatch, 'a@example.test')
    enqueue(session, 'poll_mail', {})
    run_once(session, deps, clock.now)
    assert deps.inbox.done == ['<m2@example.test>']  # m1 перечитается при следующем опросе
    assert deps.mail_failures == {'<m1@example.test>': 1}
    job = session.scalars(select(Job).where(Job.kind == 'poll_mail')).one()
    assert job.status == 'done'


def _refuse_for(deps, monkeypatch, address):
    real = deps.mailer.send

    def refused(to, *a, **kw):
        if to == address:
            raise smtplib.SMTPRecipientsRefused({to: (550, b'no such user')})
        return real(to, *a, **kw)
    monkeypatch.setattr(deps.mailer, 'send', refused)


def test_poison_mail_set_aside_with_one_alert(session, deps, settings, clock, monkeypatch):
    deps.inbox = FakeInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл'),
                            IncomingMail('<m2@example.test>', 'b@example.test', 'Re: файл')])
    _refuse_for(deps, monkeypatch, 'a@example.test')
    for i in range(7):
        enqueue(session, 'poll_mail', {})
        run_once(session, deps, clock.now + timedelta(minutes=i))
        assert '<m2@example.test>' in deps.inbox.done  # остальные письма обрабатываются
        assert ('<m1@example.test>' in deps.inbox.done) == (i >= 4)
    alerts = [m for m in deps.mailer.sent if m['to'] == DEV]
    assert len(alerts) == 1 and 'SMTPRecipientsRefused' in alerts[0]['body']
    assert '«Обработано»' in alerts[0]['body']
    assert all(j.status == 'done' for j in session.scalars(select(Job)))


def test_smtp_outage_is_not_poison_mail(session, deps, settings, run_until_idle, monkeypatch):
    deps.inbox = FakeInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл'),
                            IncomingMail('<m2@example.test>', 'b@example.test', 'Re: файл')])

    def down(*a, **kw):
        raise smtplib.SMTPServerDisconnected('gone')
    monkeypatch.setattr(deps.mailer, 'send', down)
    enqueue(session, 'poll_mail', {})
    run_until_idle(session, deps)
    job = session.scalars(select(Job).where(Job.kind == 'poll_mail')).one()
    assert (job.status, job.attempts, job.last_error) == ('failed', 5, 'SMTPServerDisconnected')
    assert deps.inbox.done == [] and deps.mail_failures == {}


def test_db_outage_is_not_poison_mail(session, deps, settings, clock, monkeypatch):
    deps.inbox = FakeInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл')])

    def db_down(*a, **kw):
        raise OperationalError('SELECT 1', {}, Exception('connection lost'))
    monkeypatch.setattr(review_handlers, 'accept_review_mail', db_down)
    enqueue(session, 'poll_mail', {})
    for i in range(3):
        run_once(session, deps, clock.now + timedelta(hours=i))
    assert deps.inbox.done == [] and deps.mail_failures == {}
    job = session.scalars(select(Job).where(Job.kind == 'poll_mail')).one()
    assert (job.status, job.attempts, job.last_error) == ('pending', 3, 'OperationalError')


@pytest.mark.parametrize('error, counted', [
    (smtplib.SMTPRecipientsRefused({'a@example.test': (550, b'no')}), True),
    (smtplib.SMTPDataError(554, b'rejected'), True),
    (smtplib.SMTPDataError(451, b'try later'), False),
    (smtplib.SMTPSenderRefused(550, b'no', 'planerka@example.test'), True),
    (smtplib.SMTPSenderRefused(421, b'busy', 'planerka@example.test'), False),
    (smtplib.SMTPServerDisconnected('gone'), False),
    (smtplib.SMTPConnectError(421, b'down'), False),
    (smtplib.SMTPAuthenticationError(535, b'bad'), False),
    (TimeoutError(), False),
    (ConnectionResetError(), False),
    (OSError('net'), False),
    (OperationalError('SELECT 1', {}, Exception()), False),
    (zipfile.BadZipFile('broken xlsx'), True),
    (ValueError('bad cell'), True),
])
def test_message_specific_errors(error, counted):
    assert review_handlers.message_specific(error) is counted


class DownInbox:
    def __init__(self, error):
        self.error = error

    def fetch_new(self):
        raise self.error

    def mark_done(self, message_id):
        raise AssertionError


def test_periodic_failure_alerted_once_per_error_type(session, deps, settings, clock):
    set_setting(session, 'jobs.max_attempts', 1, 'test')
    deps.inbox = DownInbox(OSError('imap down'))
    for i in range(3):
        enqueue(session, 'poll_mail', {})
        run_once(session, deps, clock.now + timedelta(minutes=i))
    assert len([m for m in deps.mailer.sent if m['to'] == DEV]) == 1
    deps.inbox = DownInbox(ValueError('other'))
    enqueue(session, 'poll_mail', {})
    run_once(session, deps, clock.now + timedelta(minutes=5))
    alerts = [m for m in deps.mailer.sent if m['to'] == DEV]
    assert len(alerts) == 2 and 'ValueError' in alerts[-1]['body']


def test_reminders_job_runs_reminders(session, deps, processed_source, executor, clock):
    rf = session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).one()
    enqueue(session, 'reminders', {})
    run_once(session, deps, rf.due_at + timedelta(minutes=1))
    assert rf.reminded_at is not None
    assert deps.mailer.sent[-1]['subject'] == 'Файл на уточнение: напоминание'


def test_scan_inbox_enqueues_transcribe(session, deps, env, audio, clock):
    set_setting(session, 'intake.stability_window_sec', 0, 'test')
    env.inbox_dir.mkdir(parents=True)
    (env.inbox_dir / 'запись.mp3').write_bytes(audio.read_bytes())
    for _ in range(2):  # первый проход наблюдает файл, второй — принимает
        enqueue(session, 'scan_inbox', {})
        run_once(session, deps, clock.now)
    source = session.scalars(select(Source)).one()
    job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
    assert job.payload == {'source_id': source.id}
    name = env.data_dir / 'meetings' / str(source.id) / 'inbox_name.txt'
    assert name.read_text(encoding='utf-8') == 'запись.mp3'  # имя файла — на сервере, не в БД
    assert 'запись' not in str(source.flags)


PERIODIC = {'scan_inbox', 'poll_mail', 'reminders', 'owner_digest'}


def test_periodic_jobs_enqueued_once(session, clock):
    ensure_periodic(session, clock.now)
    ensure_periodic(session, clock.now)
    jobs = session.scalars(select(Job)).all()
    assert sorted(j.kind for j in jobs) == sorted(PERIODIC)
    assert all(j.run_after == clock.now for j in jobs)


def test_periodic_next_run_after_interval(session, clock):
    set_setting(session, 'mail.poll_interval_sec', 120, 'test')
    ensure_periodic(session, clock.now)
    job = session.scalars(select(Job).where(Job.kind == 'poll_mail')).one()
    job.status = 'done'
    ensure_periodic(session, clock.now + timedelta(seconds=5))
    nxt = session.scalars(select(Job).where(Job.kind == 'poll_mail', Job.status == 'pending')).one()
    assert nxt.run_after == clock.now + timedelta(seconds=120)


def test_interrupted_job_counts_attempt(session, deps, settings, clock):
    job = enqueue(session, 'extract', {'source_id': 1})
    job.status = 'running'
    session.flush()
    assert recover_interrupted(session, deps, clock.now) == 1
    assert (job.status, job.attempts, job.last_error) == ('pending', 1, 'JobInterrupted')
    assert job.run_after == clock.now + timedelta(minutes=1)


def test_interrupted_job_at_limit_fails_with_alert(session, deps, settings, clock):
    job = enqueue(session, 'transcribe', {'source_id': 7})
    job.status, job.attempts = 'running', 4
    session.flush()
    recover_interrupted(session, deps, clock.now)
    assert (job.status, job.attempts) == ('failed', 5)
    assert 'JobInterrupted' in deps.mailer.sent[-1]['body']
    assert 'planerka requeue 7' in deps.mailer.sent[-1]['body']


def test_cycle_recovers_job_left_running_by_db_failure(session, deps_asr_down, source_ready,
                                                     clock, monkeypatch):
    state = WorkerState(needs_recovery=False)
    real = worker._retry_or_fail

    def db_down(*a, **kw):
        raise OSError('db connection lost')
    monkeypatch.setattr(worker, '_retry_or_fail', db_down)
    worker_cycle(session, deps_asr_down, state, clock.now)
    job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
    assert job.status == 'running' and state.needs_recovery

    monkeypatch.setattr(worker, '_retry_or_fail', real)
    worker_cycle(session, deps_asr_down, state, clock.now + timedelta(seconds=30))
    assert (job.status, job.attempts) == ('pending', 1)
    assert not state.needs_recovery
    periodic = session.scalars(select(Job).where(Job.kind == 'poll_mail')).all()
    assert periodic  # периодические задания снова ставятся


def test_unknown_job_kind_rejected(session):
    with pytest.raises(ValueError):
        enqueue(session, 'nonsense', {})


def test_job_logs_have_no_text(session, deps_leaky_text, source_ready, run_until_idle, caplog):
    caplog.set_level('DEBUG')
    run_until_idle(session, deps_leaky_text)
    assert 'Смирнов' not in caplog.text and 'прайс' not in caplog.text


class StuckInbox(FakeInbox):
    """mark_done падает, пока не починят папку на сервере."""
    def __init__(self, messages, broken=True):
        super().__init__(messages)
        self.broken, self.calls = broken, 0

    def mark_done(self, message_id):
        self.calls += 1
        if self.broken:
            raise MailError('IMAP: MOVE не выполнено')
        super().mark_done(message_id)


def test_reply_not_repeated_when_mark_done_fails(session, deps, settings, clock):
    deps.inbox = StuckInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл')])
    for i in range(3):
        enqueue(session, 'poll_mail', {})
        run_once(session, deps, clock.now + timedelta(hours=i))
    replies = [m for m in deps.mailer.sent if m['in_reply_to'] == '<m1@example.test>']
    assert len(replies) == 1  # подсказка ушла один раз, дальше — только перенос письма
    assert deps.inbox.calls >= 2 and deps.inbox.done == []
    deps.inbox.broken = False
    enqueue(session, 'poll_mail', {})
    run_once(session, deps, clock.now + timedelta(hours=5))
    assert deps.inbox.done == ['<m1@example.test>']
    assert '<m1@example.test>' not in deps.mail_answered
    assert len([m for m in deps.mailer.sent if m['in_reply_to'] == '<m1@example.test>']) == 1


class SetAsideInbox(FakeInbox):
    def __init__(self):
        super().__init__([])
        self.pending = ['UID 9, отпечаток 0123456789ab']

    def take_set_aside(self):
        out, self.pending = self.pending, []
        return out


def test_unparsable_mail_set_aside_alerted_once(session, deps, settings, clock):
    deps.inbox = SetAsideInbox()
    for i in range(2):
        enqueue(session, 'poll_mail', {})
        run_once(session, deps, clock.now + timedelta(minutes=i))
    alerts = [m for m in deps.mailer.sent if m['to'] == DEV]
    assert len(alerts) == 1
    assert 'UID 9, отпечаток 0123456789ab' in alerts[0]['body']
    assert '«Обработано»' in alerts[0]['body']


def test_replacing_extracted_recording_alerts_not_transcribed(session, deps, settings, env, audio,
                                                              clock):
    set_setting(session, 'intake.stability_window_sec', 0, 'test')
    env.inbox_dir.mkdir(parents=True)
    f = env.inbox_dir / 'запись.mp3'
    f.write_bytes(audio.read_bytes())
    for i in range(2):
        enqueue(session, 'scan_inbox', {})
        run_once(session, deps, clock.now + timedelta(seconds=i))
    first = session.scalars(select(Source)).one()
    first.status = 'разобран'
    session.execute(Job.__table__.delete())
    with f.open('ab') as h:
        h.write(b'\0' * 16)
    for i in range(2):
        enqueue(session, 'scan_inbox', {})
        run_once(session, deps, clock.now + timedelta(minutes=1, seconds=i))
    second = session.scalars(select(Source).where(Source.id != first.id)).one()
    assert second.status == 'ждёт решения: заменяет разобранную запись'
    assert session.scalars(select(Job).where(Job.kind == 'transcribe')).all() == []
    alerts = [m for m in deps.mailer.sent if m['subject'] == 'Планёрка: алерт scan_inbox']
    assert len(alerts) == 1 and f'planerka requeue {second.id}' in alerts[0]['body']
    assert str(first.id) in alerts[0]['body'] and 'запись.mp3' not in alerts[0]['body']
