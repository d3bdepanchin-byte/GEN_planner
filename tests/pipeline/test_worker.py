from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from planerka.db.models import Job, ReviewFile, Source
from planerka.llm.base import JsonLLMBase, LLMUnavailable
from planerka.mail.base import IncomingMail
from planerka.mail.fake import FakeInbox
from planerka.pipeline.jobs import enqueue, ensure_periodic, recover_running
from planerka.pipeline.worker import run_once
from planerka.settings import set_setting
from samples import DEV, EXPECTED_CANDIDATES, PLANERKA, TWO_CANDIDATES, tasks_of


def test_masking_uncertain_never_calls_llm(session, deps_leaky_text, source_ready, run_until_idle):
    run_until_idle(session, deps_leaky_text)
    assert deps_leaky_text.llm.requests == []
    assert session.get(Source, source_ready.id).status == 'маскирование не уверено'
    assert deps_leaky_text.mailer.sent
    assert deps_leaky_text.mailer.sent[-1]['to'] == DEV
    assert 'Смирнов' not in deps_leaky_text.mailer.sent[-1]['body']


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
    assert len(deps_no_phrase.mailer.sent) == 1


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


def test_transcriber_down_retried_then_alert(session, deps_asr_down, source_ready, run_until_idle):
    run_until_idle(session, deps_asr_down)
    job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
    assert (job.attempts, job.status) == (5, 'failed') and deps_asr_down.mailer.sent
    alert = deps_asr_down.mailer.sent[-1]
    assert alert['to'] == DEV and 'TranscriberUnavailable' in alert['body']
    assert str(source_ready.id) in alert['body']


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


def test_max_attempts_is_a_setting(session, deps_asr_down, source_ready, run_until_idle):
    set_setting(session, 'jobs.max_attempts', 2, 'test')
    run_until_idle(session, deps_asr_down)
    job = session.scalars(select(Job).where(Job.kind == 'transcribe')).one()
    assert (job.attempts, job.status) == (2, 'failed')


class DownLLM(JsonLLMBase):
    def __init__(self):
        super().__init__(3)
        self.requests: list[str] = []

    def _send(self, system: str, user: str) -> str:
        self.requests.append(user)
        raise LLMUnavailable('LLM: статус 529')


def test_llm_unavailable_retried_then_alert(session, make_deps, source_ready, run_until_idle):
    deps = make_deps(PLANERKA, llm=DownLLM())
    run_until_idle(session, deps)
    job = session.scalars(select(Job).where(Job.kind == 'extract')).one()
    assert (job.attempts, job.status) == (5, 'failed')
    assert len(deps.llm.requests) == 5
    assert session.get(Source, source_ready.id).status == 'распознан'
    assert tasks_of(session, source_ready) == []
    assert 'LLMUnavailable' in deps.mailer.sent[-1]['body']


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


def test_poll_mail_failure_not_marked_and_retried(session, deps, settings, clock, monkeypatch):
    deps.inbox = FakeInbox([IncomingMail('<m1@example.test>', 'a@example.test', 'Re: файл'),
                            IncomingMail('<m2@example.test>', 'b@example.test', 'Re: файл')])
    real = deps.mailer.send

    def flaky(to, *a, **kw):
        if to == 'a@example.test':
            raise OSError('smtp down')
        return real(to, *a, **kw)
    monkeypatch.setattr(deps.mailer, 'send', flaky)
    enqueue(session, 'poll_mail', {})
    run_once(session, deps, clock.now)
    assert deps.inbox.done == ['<m2@example.test>']
    job = session.scalars(select(Job).where(Job.kind == 'poll_mail')).one()
    assert (job.status, job.attempts) == ('pending', 1)


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


def test_recover_running_returns_jobs_to_queue(session):
    job = enqueue(session, 'extract', {'source_id': 1})
    job.status = 'running'
    session.flush()
    assert recover_running(session) == 1
    assert job.status == 'pending'


def test_unknown_job_kind_rejected(session):
    with pytest.raises(ValueError):
        enqueue(session, 'nonsense', {})


def test_job_logs_have_no_text(session, deps_leaky_text, source_ready, run_until_idle, caplog):
    caplog.set_level('DEBUG')
    run_until_idle(session, deps_leaky_text)
    assert 'Смирнов' not in caplog.text and 'прайс' not in caplog.text
