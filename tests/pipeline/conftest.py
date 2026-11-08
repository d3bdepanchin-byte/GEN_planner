"""Фикстуры конвейера: только фейки (распознавание, LLM, почта), часы под управлением теста.

Справочник и токены — из tests/conftest.py (P1 Смирнов, P2 Кузнецова, P3 Попов); источники
здесь создаются без явных id, поэтому фикстура tokens переопределена без `sources`.
"""
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from planerka.asr.base import Transcript
from planerka.asr.fake import FakeTranscriber
from planerka.core.workdays import Calendar
from planerka.db.models import Employee, Job, Source
from planerka.llm.fake import FakeLLM
from planerka.llm.prompts import load_prompt
from planerka.mail.fake import FakeInbox, FakeMailer
from planerka.masking.tokens import TokenStore
from planerka.pipeline.deps import Deps
from planerka.pipeline.jobs import enqueue
from planerka.pipeline.worker import run_once
from planerka.settings import PROJECT_ROOT, Env, set_setting
from samples import (DEV, EXPECTED_CANDIDATES, MEETING, NO_PHRASE, ONE_ON_ONE, PLANERKA, SILENCE,
                     TWO_CANDIDATES, PassThroughMasker, tasks_of)


@dataclass
class Clock:
    now: datetime


@pytest.fixture
def clock():
    return Clock(datetime.now(timezone.utc))


@pytest.fixture
def run_until_idle(clock):
    """run_once, пока есть задания; время переводится к ближайшему отложенному."""
    def run(session, deps, limit: int = 200) -> None:
        for _ in range(limit):
            if run_once(session, deps, clock.now).ran:
                continue
            nxt = session.scalar(select(func.min(Job.run_after)).where(Job.status == 'pending'))
            if nxt is None:
                return
            clock.now = max(clock.now, nxt)
        raise AssertionError('очередь не опустела')
    return run


@pytest.fixture
def tokens(session, employees):
    store = TokenStore(session)
    for e in employees:  # Смирнов=P1, Кузнецова=P2, Попов=P3
        store.employee(e.id)
    return store


@pytest.fixture
def env(tmp_path):
    return Env(database_url='postgresql://unused', data_dir=tmp_path / 'data',
               inbox_dir=tmp_path / 'inbox', service_mailbox='planerka@example.test')


@pytest.fixture(scope='session')
def cal():
    return Calendar.load(PROJECT_ROOT / 'config' / 'calendar')


@pytest.fixture
def settings(session):
    set_setting(session, 'alerts.developer_email', DEV, 'test')


@pytest.fixture
def host(session, employees, settings):
    h = employees[1]  # Кузнецова
    h.email = 'host@example.test'
    set_setting(session, 'meeting.host_employee_id', h.id, 'test')
    session.flush()
    return h


@pytest.fixture
def executor(session, employees):
    employees[0].email = 'smirnov@example.test'
    session.flush()
    return employees[0]


@pytest.fixture
def registry_owner(session):
    o = Employee(full_name='Орлова Нина', email='owner@example.test')
    session.add(o)
    session.flush()
    set_setting(session, 'registry.owner_employee_id', o.id, 'test')
    return o


@pytest.fixture(scope='session')
def audio(tmp_path_factory):
    path = tmp_path_factory.mktemp('audio') / 'tone.mp3'
    subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def source_ready(session, env, audio, host, executor, registry_owner):
    """Источник принят из приёмника, в очереди — распознавание."""
    s = Source(kind='запись', path='', sha256=uuid.uuid4().hex + uuid.uuid4().hex,
               status='получен', meeting_date=MEETING)
    session.add(s)
    session.flush()
    folder = env.data_dir / 'meetings' / str(s.id)
    folder.mkdir(parents=True)
    shutil.copyfile(audio, folder / 'original.mp3')
    s.path = str(folder / 'original.mp3')
    enqueue(session, 'transcribe', {'source_id': s.id})
    session.flush()
    return s


@pytest.fixture
def make_deps(env, cal, masker, names, tokens):
    def make(t: Transcript | None, *responses: str, masker_=None, llm=None) -> Deps:
        return Deps(env=env, cal=cal,
                    transcriber=FakeTranscriber({} if t is None else {'audio.wav': t}),
                    masker=masker_ or masker, names=names, tokens=tokens,
                    llm=llm or FakeLLM(list(responses)), mailer=FakeMailer(), inbox=FakeInbox(),
                    prompt=load_prompt('extract_tasks', 1))
    return make


@pytest.fixture
def deps(make_deps):
    return make_deps(PLANERKA, TWO_CANDIDATES, TWO_CANDIDATES)


@pytest.fixture
def deps_leaky_text(make_deps, directory):
    return make_deps(PLANERKA, TWO_CANDIDATES, masker_=PassThroughMasker(directory))


@pytest.fixture
def deps_one_on_one(make_deps):
    return make_deps(ONE_ON_ONE)


@pytest.fixture
def deps_no_phrase(make_deps):
    return make_deps(NO_PHRASE)


@pytest.fixture
def deps_silence(make_deps):
    return make_deps(SILENCE)


@pytest.fixture
def deps_asr_down(make_deps):
    return make_deps(None)


@pytest.fixture
def processed_source(session, deps, source_ready, run_until_idle):
    run_until_idle(session, deps)
    assert len(tasks_of(session, source_ready)) == EXPECTED_CANDIDATES
    return source_ready
