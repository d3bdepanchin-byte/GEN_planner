"""Фикстуры разбора: синтетический справочник и токены из tests/conftest.py (P1 Смирнов,
P2 Кузнецова, P3 Попов), дата встречи 2026-11-11. Ответы FakeLLM — в test_run.py."""
from datetime import date

import pytest

from planerka.asr.base import Segment, Transcript
from planerka.core.numbering import set_sequence_after
from planerka.core.statuses import TaskStatus
from planerka.db.models import Source, Task
from planerka.extract.run import ExtractDeps
from planerka.llm.fake import FakeLLM
from planerka.llm.prompts import load_prompt

MEETING = date(2026, 11, 11)


@pytest.fixture
def make_deps(masker, names, directory, tokens):
    def make(*responses: str) -> ExtractDeps:
        return ExtractDeps(masker=masker, names=names, directory=directory, tokens=tokens,
                           llm=FakeLLM(list(responses)), prompt=load_prompt('extract_tasks', 1))
    return make


@pytest.fixture
def source(session, sources):
    s = Source(id=100, kind='планёрка', path='synthetic/100.m4a', sha256='a' * 64,
               meeting_date=MEETING, status='распознано')
    session.add(s)
    session.flush()
    return s


@pytest.fixture
def smirnov(employees):
    return employees[0]


@pytest.fixture
def task_21(session):
    set_sequence_after(session, 21)  # как после импорта реестра: 21 уже выдан
    t = Task(number=21, title='Обновить прайс', type='поручение', priority='B',
             due_date=date(2026, 11, 18), status=TaskStatus.IN_PROGRESS.value)
    session.add(t)
    session.flush()
    return t


def _transcript(*lines: str) -> Transcript:
    return Transcript(segments=[Segment(i * 30.0, i * 30.0 + 25, ln) for i, ln in enumerate(lines)])


@pytest.fixture
def transcript():
    return _transcript(
        'Начинаем планёрку, тема — прайс-лист.',
        'Нужно подготовить прайс-лист для строительных компаний.',
        'Фиксируем итоги. Ставим задачу: подготовить прайс-лист, срок — двадцатое ноября.',
        'Итоги зафиксированы, планёрка закончена.',
    )


@pytest.fixture
def transcript_with_names():
    return _transcript(
        'Начинаем планёрку, ведёт Смирнов Александр.',
        'Ольга Кузнецова, отдел продаж. Попов Александр, склад.',
        'Фиксируем итоги. Ставим задачу: подготовить прайс-лист, ответственный — Смирнов.',
        'Саша, проверь остатки вместе с Олей.',
        'Итоги зафиксированы, планёрка закончена.',
    )
