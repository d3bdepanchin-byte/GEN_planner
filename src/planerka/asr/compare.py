"""Сравнение моделей распознавания (Д-24, Д-82).

Чистые части: метрики, выбор фрагмента, правило решения, отчёт. Прогон моделей — в
tools/asr_compare.py (только на сервере, там лежат записи и модели).

Правило решения зафиксировано до прогона. Качество в порядке, если у GigaAM ошибок
в словах не больше и имён распознано не меньше, чем у Whisper. Скорость в порядке,
если hours_per_audio_hour * meeting_hours <= window_hours. Оба условия — GIGAAM;
только качество — WAIT_FOR_GPU; иначе — WHISPER.
"""
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum

import jiwer

from planerka.asr.base import Transcript
from planerka.asr.filter import normalize

WHISPER_SIZES = ('small', 'medium', 'large-v3')  # от меньшей к большей


class Decision(str, Enum):
    GIGAAM = 'gigaam'
    WHISPER = 'whisper'
    WAIT_FOR_GPU = 'wait_for_gpu'


@dataclass
class CompareResult:
    engine: str
    model: str
    wer: float
    names_recall: float
    hours_per_audio_hour: float


def wer(reference: str, hypothesis: str) -> float:
    ref = normalize(reference)
    if not ref:
        raise ValueError('пустой эталон')
    return float(jiwer.wer(ref, normalize(hypothesis)))


def names_recall(names: list[str], hypothesis: str) -> float:
    wanted = Counter(n for n in (normalize(x) for x in names) if n)
    if not wanted:
        raise ValueError('пустой список имён')
    hyp = normalize(hypothesis)
    found = 0
    for name, need in wanted.items():
        have = len(re.findall(rf'(?<!\S){re.escape(name)}(?!\S)', hyp))
        found += min(need, have)
    return found / sum(wanted.values())


def fits_window(r: CompareResult, window_hours: float, meeting_hours: float) -> bool:
    return r.hours_per_audio_hour * meeting_hours <= window_hours


def decide(
    gigaam: CompareResult, whisper_cpu: CompareResult, window_hours: float, meeting_hours: float
) -> Decision:
    quality_ok = gigaam.wer <= whisper_cpu.wer and gigaam.names_recall >= whisper_cpu.names_recall
    if not quality_ok:
        return Decision.WHISPER
    if fits_window(gigaam, window_hours, meeting_hours):
        return Decision.GIGAAM
    return Decision.WAIT_FOR_GPU


def fragment_text(transcript: Transcript, start_sec: float, end_sec: float) -> str:
    """Текст сегментов, пересекающих интервал (касание границы не считается)."""
    if end_sec <= start_sec:
        raise ValueError('конец фрагмента не позже начала')
    return ' '.join(
        s.text.strip()
        for s in transcript.segments
        if s.end > start_sec and s.start < end_sec and s.text.strip()
    )


def pick_whisper_cpu(
    results: list[CompareResult], window_hours: float, meeting_hours: float
) -> tuple[CompareResult, bool]:
    """Самая крупная модель Whisper, которая укладывается в окно.

    Если не укладывается ни одна — самая малая, второй элемент False.
    """
    order = {m: i for i, m in enumerate(WHISPER_SIZES)}
    whisper = sorted((r for r in results if r.engine == 'whisper'), key=lambda r: order[r.model])
    if not whisper:
        raise ValueError('нет результатов Whisper')
    fitting = [r for r in whisper if fits_window(r, window_hours, meeting_hours)]
    return (fitting[-1], True) if fitting else (whisper[0], False)


def pick_gigaam(results: list[CompareResult]) -> CompareResult:
    """Лучший вариант GigaAM: меньше ошибок, затем больше имён, затем быстрее."""
    giga = [r for r in results if r.engine == 'gigaam']
    if not giga:
        raise ValueError('нет результатов GigaAM')
    return min(giga, key=lambda r: (r.wer, -r.names_recall, r.hours_per_audio_hour))


_VERDICT = {
    Decision.GIGAAM: 'GigaAM укладывается в окно и не хуже Whisper по качеству.',
    Decision.WAIT_FOR_GPU: 'GigaAM не хуже по качеству, но не укладывается в окно на CPU. Ждём GPU.',
    Decision.WHISPER: 'GigaAM хуже Whisper по качеству. Берём Whisper.',
}


def render_report(
    date: str,
    results: list[CompareResult],
    decision: Decision,
    window_hours: float,
    meeting_hours: float,
    *,
    whisper_cpu: CompareResult,
    gigaam: CompareResult,
    whisper_cpu_fits: bool = True,
) -> str:
    """Отчёт: только метрики, текста записей в нём нет."""
    lines = [
        f'# Сравнение моделей распознавания, {date}',
        '',
        f'Окно ночной обработки: {window_hours:g} ч. Длина встречи для оценки скорости: {meeting_hours:g} ч.',
        '',
        '| Движок | Модель | WER | Полнота имён | ч обработки на ч аудио | Встреча в окне | '
        'Полнота задач против протокола |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ]
    for r in results:
        fit = 'да' if fits_window(r, window_hours, meeting_hours) else 'нет'
        lines.append(
            f'| {r.engine} | {r.model} | {r.wer:.3f} | {r.names_recall:.3f} | '
            f'{r.hours_per_audio_hour:.2f} | {fit} | |'
        )
    lines += [
        '',
        f'База whisper_cpu: {whisper_cpu.model}.',
    ]
    if not whisper_cpu_fits:
        lines.append('Ни одна модель Whisper не укладывается в окно. Взята самая малая.')
    lines.append('Модель large-v3 в отчёте для справки, если она не выбрана базой.')
    lines += [
        f'Лучший GigaAM: {gigaam.model}.',
        '',
        f'Решение: {decision.name}. {_VERDICT[decision]}',
        '',
        'Колонку «полнота задач против протокола» заполняет владелец реестра после задачи 15.',
        '',
    ]
    return '\n'.join(lines)
