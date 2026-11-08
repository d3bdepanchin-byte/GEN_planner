"""Осознанная переобработка (Д-51): новая стенограмма и разбор рядом с текущими, без новых задач.

Результат — в data/meetings/<id>/reprocess_<ГГГГммдд-ЧЧММСС>/ на сервере: transcript.json,
result.json (кандидаты и правки с ФИО) и comparison.md — сравнение с текущими задачами
источника. Task и TaskEdit не создаются, статус источника не меняется. Маскирование
и вторая проверка — как в обычном разборе: при остатке ПДн в LLM ничего не уходит.
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.asr.base import NoSpeech, Transcript
from planerka.asr.filter import MeetingKind, detect_meeting_kind
from planerka.core.directory import norm_name
from planerka.core.numbering import format_number
from planerka.db.models import Employee, Source, Task, TaskEdit
from planerka.extract.run import Analysis, ExtractDeps, analyze_source
from planerka.intake.audio import extract_audio
from planerka.pipeline.deps import Deps
from planerka.pipeline.handlers.meeting import opening_phrases
from planerka.pipeline.transcripts import (TRANSCRIPT_FILE, load_transcript, meeting_dir,
                                           save_transcript)
from planerka.reminders import company_tz
from planerka.settings import get_setting

log = logging.getLogger(__name__)


def reprocess(session: Session, source_id: int, deps: Deps) -> Path:
    source = session.get(Source, source_id)
    if source is None:
        raise ValueError(f'источника {source_id} нет')
    folder = meeting_dir(deps.env, source.id)
    out = folder / f'reprocess_{datetime.now(company_tz(session)):%Y%m%d-%H%M%S}'
    out.mkdir(parents=True)
    wav = extract_audio(Path(source.path), out)
    try:
        transcript = deps.transcriber.transcribe(wav)
    finally:
        wav.unlink(missing_ok=True)
    save_transcript(out, transcript)
    try:
        kind = detect_meeting_kind(transcript, opening_phrases(session),
                                   int(get_setting(session, 'intake.opening_window_sec')))
        kind_label = kind.value if kind is not None else 'нет фразы открытия'
    except NoSpeech:
        kind, kind_label = None, 'нет речи'

    analysis = Analysis(candidates=[], edits=[], summary='', final_block_found=False, prompt='',
                        notes=[f'разбор не выполнен: {kind_label}'])
    if kind is MeetingKind.PLANERKA:  # как в конвейере: дальше распознавания идут только планёрки
        analysis = analyze_source(session, source, transcript, ExtractDeps(
            masker=deps.masker, names=deps.names, directory=deps.masker.directory,
            tokens=deps.tokens, llm=deps.llm, prompt=deps.prompt))
    names = {e.id: e.full_name for e in session.scalars(select(Employee))}
    (out / 'result.json').write_text(json.dumps(
        _result(analysis, names, kind_label), ensure_ascii=False, indent=1, default=_iso),
        encoding='utf-8')
    current = list(session.scalars(select(Task).where(Task.source_id == source.id)
                                   .order_by(Task.number)))
    previous = _previous_transcript(folder)
    (out / 'comparison.md').write_text(
        _comparison(source, previous, transcript, analysis, current, names), encoding='utf-8')
    log.info('переобработка источника %s: кандидатов %d, текущих задач %d', source.id,
             len(analysis.candidates), len(current))
    return out


def _iso(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _task_dict(t: Task, names: dict[int, str]) -> dict:
    return {
        'title': t.title, 'type': t.type, 'priority': t.priority, 'goal_2027': t.goal_2027,
        'owner': names.get(t.owner_id), 'coexecutors': [names.get(i) for i in t.coexecutor_ids],
        'author': names.get(t.author_id), 'due_date': t.due_date, 'control_date': t.control_date,
        'done_criteria': t.done_criteria, 'timecode': t.timecode, 'quote': t.quote,
        'confidence': t.confidence, 'confidence_reason': t.confidence_reason,
        'questions': list(t.questions), 'flags': list(t.flags),
        'registry_suggested': t.registry_suggested,
    }


def _edit_dict(e: TaskEdit) -> dict:
    return {'task': format_number(e.task_number), 'change': e.change, 'quote': e.quote,
            'timecode': e.timecode}


def _result(a: Analysis, names: dict[int, str], kind: str) -> dict:
    return {'meeting_kind': kind, 'prompt': a.prompt, 'final_block_found': a.final_block_found,
            'summary': a.summary, 'candidates': [_task_dict(t, names) for t in a.candidates],
            'edits': [_edit_dict(e) for e in a.edits], 'notes': a.notes}


def _previous_transcript(folder: Path) -> Transcript | None:
    return load_transcript(folder) if (folder / TRANSCRIPT_FILE).is_file() else None


def _similarity(a: str, b: str) -> int:
    return round(fuzz.token_set_ratio(norm_name(a), norm_name(b)))


def _cell(text: str | None) -> str:
    return (text or '—').replace('|', '/').replace('\n', ' ')


def _comparison(source: Source, previous: Transcript | None, new: Transcript, a: Analysis,
                current: list[Task], names: dict[int, str]) -> str:
    """Для каждого нового кандидата — самая похожая текущая задача и сходство названий, %.

    Решение «та же задача или нет» принимает человек: порога здесь нет.
    """
    lines = [f'# Переобработка источника {source.id}', '']
    lines += ['| | Было | Стало |', '| --- | --- | --- |']
    if previous is not None:
        lines.append(f'| Распознавание | {previous.engine} {previous.model} | '
                     f'{new.engine} {new.model} |')
        lines.append(f'| Сегментов | {len(previous.segments)} | {len(new.segments)} |')
    else:
        lines.append(f'| Распознавание | — | {new.engine} {new.model} |')
    lines.append(f'| Задач и кандидатов | {len(current)} | {len(a.candidates)} |')
    if a.notes:
        lines += ['', *(f'Заметка: {n}' for n in a.notes)]
    lines += ['', '## Новые кандидаты и самая похожая текущая задача', '',
              '| Кандидат | Ответственный | Срок | Похожая задача | Сходство, % |',
              '| --- | --- | --- | --- | --- |']
    paired: set[int] = set()
    for t in a.candidates:
        best = max(current, key=lambda c: _similarity(t.title, c.title), default=None)
        if best is not None:
            paired.add(best.number)
            match = f'{format_number(best.number)} {_cell(best.title)}'
            score = str(_similarity(t.title, best.title))
        else:
            match, score = '—', '—'
        lines.append(f'| {_cell(t.title)} | {_cell(names.get(t.owner_id))} | '
                     f'{t.due_date or "—"} | {match} | {score} |')
    lines += ['', '## Текущие задачи источника', '',
              '| Номер | Задача | Ответственный | Срок | Статус | Есть похожий кандидат |',
              '| --- | --- | --- | --- | --- | --- |']
    for c in current:
        lines.append(f'| {format_number(c.number)} | {_cell(c.title)} | '
                     f'{_cell(names.get(c.owner_id))} | {c.due_date or "—"} | {c.status} | '
                     f'{"да" if c.number in paired else "нет"} |')
    if a.edits:
        lines += ['', '## Правки к существующим задачам', '']
        lines += [f'- {format_number(e.task_number)}: {_cell(e.change)}' for e in a.edits]
    return '\n'.join(lines) + '\n'
