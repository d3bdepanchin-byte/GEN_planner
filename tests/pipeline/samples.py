"""Синтетические данные конвейера: стенограммы, ответы FakeLLM, помощники тестов."""
import json
from datetime import date

from sqlalchemy import select

from planerka.asr.base import Segment, Transcript, TranscriptionFailed
from planerka.db.models import Task
from planerka.masking.masker import MaskResult

MEETING = date(2026, 11, 11)
DEV = 'dev@example.test'
INBOX_NAME = 'планёрка 11 ноября.m4a'


def transcript(*lines: str) -> Transcript:
    """Сегмент i начинается на i*30 с: таймкоды [00:00], [00:30], [01:00]…"""
    return Transcript(segments=[Segment(i * 30.0, i * 30.0 + 25, ln) for i, ln in enumerate(lines)],
                      engine='fake', model='fake-1', audio_sec=60.0, processing_sec=1.0)


PLANERKA = transcript(
    'Начинаем планёрку, тема — прайс-лист.',
    'Фиксируем итоги. Ставим задачу: подготовить прайс-лист, ответственный — Смирнов.',
    'Ставим задачу: сверить остатки на складе.',
)
ONE_ON_ONE = transcript('Встреча один на один, обсуждаем план на месяц.')
NO_PHRASE = transcript('Обсуждаем погоду и отпуск.')
SILENCE = Transcript(segments=[], engine='fake', model='fake-1')


def candidate(**over) -> dict:
    c = {
        'title': 'Подготовить прайс-лист для строительных компаний',
        'type': 'поручение', 'priority': 'B', 'goal_2027': 'стратегия продаж',
        'owner': '[P1]', 'coexecutors': [], 'author': None,
        'due_date': '2026-11-20', 'control_date': None,
        'done_criteria': 'прайс-лист отправлен в отдел продаж',
        'timecode': '[00:30]',
        'quote': 'Ставим задачу: подготовить прайс-лист, ответственный — [P1]',
        'confidence': 'высокая', 'confidence_reason': 'задача повторена в итогах',
        'questions': [], 'from_final_block': True, 'closed_registry_suggested': False,
    }
    c.update(over)
    return c


def llm_result(candidates: list[dict]) -> str:
    return json.dumps({'final_block_found': True, 'summary': 'Обсудили прайс-лист, готовит [P1].',
                       'candidates': candidates, 'edits': []}, ensure_ascii=False)


# Два кандидата: у первого ответственный [P1], у второго ответственного нет (ждёт владельца).
TWO_CANDIDATES = llm_result([
    candidate(),
    candidate(title='Сверить остатки на складе', owner=None, timecode='[01:00]',
              quote='Ставим задачу: сверить остатки на складе.'),
])
EXPECTED_CANDIDATES = 2



class BrokenFileTranscriber:
    """Запись не распознаётся (битый файл): ошибка не временная."""

    def transcribe(self, wav):
        raise TranscriptionFailed('broken')


class PassThroughMasker:
    """Маскировщик, который ничего не заменил: вторая проверка обязана остановить текст."""

    def __init__(self, directory):
        self.directory = directory

    def mask(self, text: str, source_id: int) -> MaskResult:
        return MaskResult(text)


def tasks_of(session, source) -> list[Task]:
    return list(session.scalars(select(Task).where(Task.source_id == source.id)
                                .order_by(Task.number)))
