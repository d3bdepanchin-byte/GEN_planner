"""Разбор записи: маскирование → вторая проверка → LLM → расшифровка → постобработка → сохранение.

`analyze_source` — всё до сохранения: кандидаты и правки без номеров, в сессию не добавлены
(переобработка, Д-51). `extract_source` — то же плюс номера И-NNN и запись в ядро.

Во внешний LLM уходит только текст, прошедший `ensure_clean` (правило 5): при остатке ПДн
`MaskingUncertain` выходит наружу до запроса. Ответ LLM — недоверенный: люди резолвятся из токена
прямо в сотрудника (R18), текстовые поля — через `demask_text` (R19). Токен и таймкод принимаются,
только если они есть в замаскированной записи. Пробелы не додумываются (правило 2): поле пустое,
вопрос в `questions`. Заметки отчёта и логи — без ПДн.
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
from planerka.masking.demask import NOT_IN_RECORDING, UNKNOWN, demask_text
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
_TIMECODE = re.compile(r'\[\d{2,}:\d{2}\]')
NO_TIMECODE = 'таймкод не найден в записи'
NO_TIMECODE_GIVEN = 'таймкод не указан'
NO_OWNER, NO_TYPE = 'ответственный не назван', 'тип не указан'
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


@dataclass
class Analysis:
    """Результат разбора до сохранения: задачи без номеров, в сессию не добавлены."""
    candidates: list[Task]
    edits: list[TaskEdit]
    summary: str
    final_block_found: bool
    prompt: str  # «extract_tasks.v1»
    notes: list[str] = field(default_factory=list)  # без ПДн


@dataclass
class _Recording:
    """Что есть в замаскированной записи: только этим токенам и таймкодам можно верить."""
    source: Source
    tokens: TokenStore
    seen: set[str]
    timecodes: set[str]

    def text(self, value: str, notes: list[str]) -> str:
        out, found = demask_text(value, self.tokens, self.source.id, self.seen)
        notes += found
        return out

    def timecode(self, value: str | None) -> tuple[str | None, str | None]:
        """(таймкод или None, вопрос или None). Источник задачи — таймкод и цитата: пустой
        таймкод не додумывается (правило 2), а становится вопросом."""
        if value is None or not value.strip():
            return None, NO_TIMECODE_GIVEN
        ok = _TIMECODE.fullmatch(value) is not None and value in self.timecodes
        return (value, None) if ok else (None, NO_TIMECODE)


def analyze_source(session: Session, source: Source, transcript: Transcript,
                   deps: ExtractDeps) -> Analysis:
    """Кандидаты и правки из стенограммы без сохранения: Task и TaskEdit не создаются в БД.

    Токены внешних людей и контактов при маскировании сохраняются (TokenStore).
    """
    masked = deps.masker.mask(transcript.text, source.id)
    ensure_clean(masked.text, deps.directory, deps.names)
    goals: list[str] = list(get_setting(session, 'goals.goals_2027'))
    meeting = source.meeting_date
    result: ExtractionResult = deps.llm.complete_json(deps.prompt, {
        'meeting_date': meeting.isoformat() if meeting else 'неизвестна',
        'goals': '\n'.join(f'- {g}' for g in goals),
        'transcript': masked.text,
    }, ExtractionResult)
    rec = _Recording(source, deps.tokens, set(TOKEN_IN_TEXT.findall(masked.text)),
                     set(_TIMECODE.findall(masked.text)))

    report_notes: list[str] = []
    edits = [e for e in (_task_edit(session, rec, edit, report_notes) for edit in result.edits)
             if e is not None]  # до новых задач: правка — только к уже существующему номеру

    post = _Postprocess(goals, meeting)
    created = [_candidate_task(session, rec, c, result.final_block_found, post)
               for c in result.candidates]
    _check_priority_a(session, created, int(get_setting(session, 'registry.max_priority_a')))

    notes: list[str] = []
    summary = rec.text(result.summary, notes)
    report_notes += [f'саммари: {_kind(n)}' for n in notes]
    return Analysis(candidates=created, edits=edits, summary=summary,
                    final_block_found=result.final_block_found,
                    prompt=f'{deps.prompt.name}.v{deps.prompt.version}', notes=report_notes)


def extract_source(session: Session, source: Source, transcript: Transcript,
                   deps: ExtractDeps) -> ExtractReport:
    """Кандидаты задач и правки из стенограммы. Разобранный источник повторно не разбирается."""
    report = ExtractReport()
    if source.status == EXTRACTED:
        return report

    analysis = analyze_source(session, source, transcript, deps)
    for edit in analysis.edits:
        session.add(edit)
    for task in analysis.candidates:
        task.number = next_task_number(session)
        session.add(task)
    session.flush()

    flags = dict(source.flags or {})
    flags['prompt'] = analysis.prompt
    if not analysis.final_block_found:
        flags[NO_FINAL_BLOCK] = True
    source.flags = flags
    source.summary = analysis.summary
    source.status = EXTRACTED
    session.flush()

    report.created = [t.number for t in analysis.candidates]
    report.edits = len(analysis.edits)
    report.notes = analysis.notes
    log.info('источник %s разобран: кандидатов %d, правок %d, заметок %d',
             source.id, len(report.created), report.edits, len(report.notes))
    return report


def _kind(note: str) -> str:
    """Заметка без ФИО для отчёта: «выбрать: …» — просто неоднозначный токен."""
    return (note if note in (UNKNOWN, NOT_IN_RECORDING, NO_TIMECODE, NO_TIMECODE_GIVEN)
            else 'неоднозначный токен')


def _task_edit(session: Session, rec: _Recording, edit: EditOut,
               report_notes: list[str]) -> TaskEdit | None:
    """Правка к существующей задаче, в сессию не добавлена; иначе None и заметка."""
    try:
        number = parse_number(edit.task_number)
    except ValueError:
        report_notes.append('правка: не номер задачи')
        return None
    label = format_number(number)
    if session.scalar(select(Task.id).where(Task.number == number)) is None:
        report_notes.append(f'правка к несуществующей задаче {label}')
        return None
    notes: list[str] = []
    change, quote = rec.text(edit.change, notes), rec.text(edit.quote, notes)
    timecode, problem = rec.timecode(edit.timecode)
    if problem:
        notes.append(problem)
    report_notes += [f'правка к {label}: {_kind(n)}' for n in dict.fromkeys(notes)]
    return TaskEdit(task_number=number, change=change, quote=quote or None, timecode=timecode,
                    source_id=rec.source.id, status=TaskStatus.CANDIDATE.value)


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


def _candidate_task(session: Session, rec: _Recording, c: CandidateOut, final_block_found: bool,
                    post: _Postprocess) -> Task:
    questions: list[str] = []
    owner_id = _person(session, rec, c.owner, _OWNER, questions)
    author_id = _person(session, rec, c.author, _AUTHOR, questions)
    coexecutor_ids: list[int] = []
    for value in c.coexecutors:
        emp = _person(session, rec, value, _COEXECUTOR, questions)
        if emp is not None and emp not in coexecutor_ids:
            coexecutor_ids.append(emp)
    if c.owner is None or not c.owner.strip():
        questions.append(NO_OWNER)
    if c.type is None:
        questions.append(NO_TYPE)

    notes: list[str] = []
    title, quote = rec.text(c.title, notes), rec.text(c.quote, notes)
    done = rec.text(c.done_criteria, notes) if c.done_criteria is not None else None
    reason = rec.text(c.confidence_reason, notes)
    asked = [q for q in (rec.text(q, notes) for q in c.questions) if q.strip()]
    timecode, problem = rec.timecode(c.timecode)
    if problem:
        questions.append(problem)
    flags: list[str] = []
    if not final_block_found:
        flags.append(NO_FINAL_BLOCK)
    if not (final_block_found and c.from_final_block):
        flags.append(FROM_DISCUSSION)

    task = Task(  # номер выдаёт extract_source при сохранении
        title=title, type=c.type,
        priority=c.priority, goal_2027=c.goal_2027, author_id=author_id, owner_id=owner_id,
        coexecutor_ids=coexecutor_ids, due_date=c.due_date, control_date=c.control_date,
        done_criteria=done or None, source_id=rec.source.id, timecode=timecode,
        quote=quote, confidence=c.confidence, confidence_reason=reason, flags=flags,
        source_closed=rec.source.closed,
        registry_suggested=CLOSED_REGISTRY if c.closed_registry_suggested else None,
        status=TaskStatus.CANDIDATE.value,
    )
    task.questions = list(dict.fromkeys(asked + questions + notes + post.apply(task)))
    return task


def _person(session: Session, rec: _Recording, value: str | None, role: tuple[str, str],
            questions: list[str]) -> int | None:
    """Сотрудник по токену [Pn] из записи; иначе None и вопрос. ФИО — только в вопросе выбора."""
    if value is None or not value.strip():
        return None
    unknown = f'{role[0]}: {UNKNOWN}'
    value = value.strip()
    if TOKEN_IN_TEXT.fullmatch(value) is None:
        questions.append(unknown)
        return None
    if value not in rec.seen:  # LLM выдумал человека или выбрал одного из [Pa|Pb]
        questions.append(f'{role[0]}: {NOT_IN_RECORDING}')
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
        surface = rec.tokens.resolve(rec.source.id, value)
        questions.append(f'{role[0]} не из справочника: {surface}' if surface else unknown)
        return None
    if len(parts) > 1 and kinds == {'P'}:
        names = [rec.tokens.resolve(rec.source.id, f'[{p.group()}]') for p in parts]
        questions.append(unknown if None in names
                         else f'выбрать {role[1]}: ' + ' или '.join(names))
        return None
    questions.append(unknown)  # контакт или смесь видов вместо человека
    return None


def _check_priority_a(session: Session, created: list[Task], limit: int) -> None:
    """Больше `limit` незавершённых задач A у человека — только вопрос (Д-79).

    Новые задачи ещё не в БД: считаются сохранённые плюс новые.
    """
    owners = {t.owner_id for t in created if t.priority == 'A' and t.owner_id is not None}
    for owner_id in owners:
        count = session.scalar(select(func.count()).select_from(Task).where(
            Task.owner_id == owner_id, Task.priority == 'A', Task.status.in_(_OPEN)))
        count += sum(1 for t in created if t.owner_id == owner_id and t.priority == 'A')
        if count <= limit:
            continue
        note = f'у ответственного больше {limit} задач A ({count}) — пересмотреть приоритеты'
        for t in created:
            if t.owner_id == owner_id and t.priority == 'A' and note not in t.questions:
                t.questions = [*t.questions, note]
