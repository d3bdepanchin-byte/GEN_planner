"""Разбор записи: маскирование → вторая проверка → LLM → расшифровка → постобработка → сохранение.

Во внешний LLM уходит только текст, прошедший `ensure_clean` (правило 5): при остатке ПДн
`MaskingUncertain` выходит наружу до запроса. Ответ LLM — недоверенный: люди резолвятся из токена
прямо в сотрудника (R18), текстовые поля — через `demask`. Пробелы не додумываются (правило 2):
поле пустое, вопрос в `questions`. Заметки отчёта и логи — без ПДн.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planerka.asr.base import Transcript
from planerka.core.directory import EmployeeRef
from planerka.core.numbering import format_number, next_task_number, parse_number
from planerka.core.statuses import TaskStatus
from planerka.db.models import Source, Task, TaskEdit, Token
from planerka.extract.schema import CandidateOut, EditOut, ExtractionResult
from planerka.llm.base import LLMClient
from planerka.llm.prompts import Prompt
from planerka.masking.checker import ensure_clean
from planerka.masking.demask import UNKNOWN, demask
from planerka.masking.masker import Masker
from planerka.masking.names import NameFinder
from planerka.masking.tokens import TOKEN_IN_TEXT, TokenStore
from planerka.settings import get_setting

log = logging.getLogger(__name__)

EXTRACTED = 'разобран'
NO_FINAL_BLOCK = 'итоги не зафиксированы'
FROM_DISCUSSION = 'кандидат из обсуждения'
CLOSED_REGISTRY = 'закрытый'
TYPE_ORDER, TYPE_PROJECT, TYPE_INVESTIGATION, TYPE_RULE = (
    'поручение', 'проект', 'разбирательство', 'регламент')
# Незавершённые задачи: в лимит задач A на человека (Схема, Д-79).
_OPEN = tuple(s.value for s in (TaskStatus.CANDIDATE, TaskStatus.CONFIRMED_BY_EXECUTOR,
                                TaskStatus.IN_PROGRESS, TaskStatus.PAUSED))
_PART = re.compile(r'([A-Z]+)(\d+)')
# Роль поля с человеком: (именительный, родительный) — для вопросов.
_OWNER, _COEXECUTOR, _AUTHOR = (('ответственный', 'ответственного'),
                                ('соисполнитель', 'соисполнителя'),
                                ('постановщик', 'постановщика'))


@dataclass
class ExtractDeps:
    masker: Masker
    names: NameFinder
    directory: list[EmployeeRef]
    tokens: TokenStore
    llm: LLMClient
    prompt: Prompt


@dataclass
class ExtractReport:
    created: list[int] = field(default_factory=list)  # номера созданных задач
    edits: int = 0
    notes: list[str] = field(default_factory=list)  # без ПДн: номера и виды проблем


def extract_source(session: Session, source: Source, transcript: Transcript,
                   deps: ExtractDeps) -> ExtractReport:
    """Кандидаты задач и правки из стенограммы. Разобранный источник повторно не разбирается."""
    report = ExtractReport()
    if source.status == EXTRACTED:
        return report

    masked = deps.masker.mask(transcript.text, source.id)
    ensure_clean(masked.text, deps.directory, deps.names)
    goals: list[str] = list(get_setting(session, 'goals.goals_2027'))
    meeting = source.meeting_date
    result: ExtractionResult = deps.llm.complete_json(deps.prompt, {
        'meeting_date': meeting.isoformat() if meeting else 'неизвестна',
        'goals': '\n'.join(f'- {g}' for g in goals),
        'transcript': masked.text,
    }, ExtractionResult)

    for edit in result.edits:  # до новых задач: правка — только к уже существующему номеру
        _save_edit(session, source, edit, deps.tokens, report)

    post = _Postprocess(goals, meeting)
    created: list[Task] = []
    for c in result.candidates:
        task = _candidate_task(session, source, c, result.final_block_found, deps.tokens, post)
        session.add(task)
        created.append(task)
    session.flush()
    _check_priority_a(session, created, int(get_setting(session, 'registry.max_priority_a')))

    summary, notes = demask(result.summary, deps.tokens, source.id)
    report.notes += [f'саммари: {_kind(n)}' for n in notes]
    flags = dict(source.flags or {})
    flags['prompt'] = f'{deps.prompt.name}.v{deps.prompt.version}'
    if not result.final_block_found:
        flags[NO_FINAL_BLOCK] = True
    source.flags = flags
    source.summary = summary
    source.status = EXTRACTED
    session.flush()

    report.created = [t.number for t in created]
    log.info('источник %s разобран: кандидатов %d, правок %d, заметок %d',
             source.id, len(report.created), report.edits, len(report.notes))
    return report


def _kind(note: str) -> str:
    """Заметка demask без ФИО: «выбрать: …» в отчёте — просто неоднозначный токен."""
    return UNKNOWN if note == UNKNOWN else 'неоднозначный токен'


def _save_edit(session: Session, source: Source, edit: EditOut, tokens: TokenStore,
               report: ExtractReport) -> None:
    try:
        number = parse_number(edit.task_number)
    except ValueError:
        report.notes.append('правка: не номер задачи')
        return
    label = format_number(number)
    if session.scalar(select(Task.id).where(Task.number == number)) is None:
        report.notes.append(f'правка к несуществующей задаче {label}')
        return
    data, notes = demask({'change': edit.change, 'quote': edit.quote}, tokens, source.id)
    report.notes += [f'правка к {label}: {_kind(n)}' for n in notes]
    session.add(TaskEdit(task_number=number, change=data['change'], quote=data['quote'] or None,
                         timecode=edit.timecode, source_id=source.id,
                         status=TaskStatus.CANDIDATE.value))
    report.edits += 1


def _norm(text: str) -> str:
    return text.strip().casefold().replace('ё', 'е')


class _Postprocess:
    """Правило 2 и обязательные поля по типу: неверное поле очищает, возвращает вопросы."""

    def __init__(self, goals: list[str], meeting: date | None) -> None:
        self.goals = {_norm(g): g for g in goals}
        self.meeting = meeting

    def apply(self, t: Task) -> list[str]:
        q: list[str] = []
        if t.goal_2027 is not None:
            t.goal_2027 = self.goals.get(_norm(t.goal_2027))
            if t.goal_2027 is None:
                q.append('цель 2027 не из списка — выбрать из списка')
        if self.meeting is not None:
            if t.due_date is not None and t.due_date < self.meeting:
                t.due_date = None
                q.append('срок раньше даты встречи — уточнить срок')
            if t.control_date is not None and t.control_date < self.meeting:
                t.control_date = None
                q.append('контрольная дата раньше даты встречи — уточнить')
        if t.type == TYPE_ORDER:
            if t.due_date is None:
                q.append('поручение без срока — назвать срок')
            if t.done_criteria is None:
                q.append('поручение без критерия готовности — уточнить критерий')
        if t.type in (TYPE_PROJECT, TYPE_INVESTIGATION) and t.control_date is None:
            q.append('нет контрольной даты')
        if t.type == TYPE_RULE and t.priority is not None:
            t.priority = None
            q.append('приоритет регламенту не ставится — снят')
        return q


def _candidate_task(session: Session, source: Source, c: CandidateOut, final_block_found: bool,
                    tokens: TokenStore, post: _Postprocess) -> Task:
    questions: list[str] = []
    owner_id = _person(session, tokens, source.id, c.owner, _OWNER, questions)
    author_id = _person(session, tokens, source.id, c.author, _AUTHOR, questions)
    coexecutor_ids: list[int] = []
    for value in c.coexecutors:
        emp = _person(session, tokens, source.id, value, _COEXECUTOR, questions)
        if emp is not None and emp not in coexecutor_ids:
            coexecutor_ids.append(emp)

    text, notes = demask({'title': c.title, 'quote': c.quote, 'done_criteria': c.done_criteria,
                          'confidence_reason': c.confidence_reason, 'questions': c.questions},
                         tokens, source.id)
    asked = [q for q in text['questions'] if q.strip()]
    flags: list[str] = []
    if not final_block_found:
        flags.append(NO_FINAL_BLOCK)
    if not (final_block_found and c.from_final_block):
        flags.append(FROM_DISCUSSION)

    task = Task(
        number=next_task_number(session), title=text['title'], type=c.type,
        priority=c.priority, goal_2027=c.goal_2027, author_id=author_id, owner_id=owner_id,
        coexecutor_ids=coexecutor_ids, due_date=c.due_date, control_date=c.control_date,
        done_criteria=text['done_criteria'] or None, source_id=source.id, timecode=c.timecode,
        quote=text['quote'], confidence=c.confidence,
        confidence_reason=text['confidence_reason'], flags=flags,
        source_closed=source.closed,
        registry_suggested=CLOSED_REGISTRY if c.closed_registry_suggested else None,
        status=TaskStatus.CANDIDATE.value,
    )
    questions = asked + questions + notes + post.apply(task)
    task.questions = list(dict.fromkeys(questions))
    return task


def _person(session: Session, tokens: TokenStore, source_id: int, value: str | None,
            role: tuple[str, str], questions: list[str]) -> int | None:
    """Сотрудник по токену [Pn]; иначе None и вопрос. ФИО — только в вопросе выбора."""
    if value is None or not value.strip():
        return None
    unknown = f'{role[0]}: {UNKNOWN}'
    value = value.strip()
    if TOKEN_IN_TEXT.fullmatch(value) is None:
        questions.append(unknown)
        return None
    parts = [_PART.fullmatch(p) for p in value[1:-1].split('|')]
    kinds = {p.group(1) for p in parts}
    if len(parts) == 1 and kinds == {'P'}:
        emp = session.scalar(select(Token.employee_id).where(
            Token.source_id.is_(None), Token.token == value, Token.employee_id.is_not(None)))
        if emp is None:
            questions.append(unknown)
        return emp
    if len(parts) == 1 and kinds == {'X'}:
        surface = tokens.resolve(source_id, value)
        questions.append(f'{role[0]} не из справочника: {surface}' if surface else unknown)
        return None
    if len(parts) > 1 and kinds == {'P'}:
        names = [tokens.resolve(source_id, f'[{p.group()}]') for p in parts]
        questions.append(unknown if None in names
                         else f'выбрать {role[1]}: ' + ' или '.join(names))
        return None
    questions.append(unknown)  # контакт или смесь видов вместо человека
    return None


def _check_priority_a(session: Session, created: list[Task], limit: int) -> None:
    """Больше `limit` незавершённых задач A у человека — только вопрос (Д-79)."""
    owners = {t.owner_id for t in created if t.priority == 'A' and t.owner_id is not None}
    for owner_id in owners:
        count = session.scalar(select(func.count()).select_from(Task).where(
            Task.owner_id == owner_id, Task.priority == 'A', Task.status.in_(_OPEN)))
        if count <= limit:
            continue
        note = f'у ответственного больше {limit} задач A ({count}) — пересмотреть приоритеты'
        for t in created:
            if t.owner_id == owner_id and t.priority == 'A' and note not in t.questions:
                t.questions = [*t.questions, note]
