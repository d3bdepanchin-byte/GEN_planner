"""Файл владельцу реестра: утвердить или отклонить задачи (Д-72, Д-74).

Пустое «Решение» — строка остаётся как есть. Что можно утвердить, решает раздел задачи по
данным в БД на момент приёма, а не ячейка «Раздел» (F5). «Задача» и «Срок» владельца
применяются при «утверждаю» (F6). Правки к задачам из записей — лист для сведения (F7).
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.core.directory import norm_name
from planerka.core.numbering import format_number
from planerka.core.statuses import TaskStatus, transition
from planerka.db.models import Employee, History, ReviewFile, Task, TaskEdit
from planerka.review.executor import (
    FLAG_REASSIGNED, FLAG_REJECTED, ApplyReport, ReviewErrors, ReviewRejected, _add_unique, _edit,
)
from planerka.review.xlsx import (
    NOT_FROM_SYSTEM, add_employee_dropdown, add_list_validation, cell_number, cell_text,
    employee_index, parse_date, read_meta, style_sheet, write_meta,
)

__all__ = ["OwnerDecision", "OwnerRow", "ParsedOwnerReview", "pending_owner_items",
           "pending_task_edits", "build_owner_file", "parse_owner_file", "apply_owner_review"]

SECTION_CONFIRMED = "подтверждена исполнителем"
SECTION_AMBIGUOUS = "спорная: неоднозначное имя"
SECTION_NO_OWNER = "спорная: нет ответственного"
SECTION_REASSIGNED = "переназначена"
SECTION_REJECTED = "отклонена исполнителем"
SECTION_NOT_RETURNED = "не вернулся файл"
SECTIONS = [SECTION_CONFIRMED, SECTION_AMBIGUOUS, SECTION_NO_OWNER, SECTION_REASSIGNED,
            SECTION_REJECTED, SECTION_NOT_RETURNED]
DISPUTED = (SECTION_AMBIGUOUS, SECTION_NO_OWNER)
# Утвердить можно только с ответственным, выбранным владельцем: в файле ячейка пустая.
CHOOSE_OWNER = (*DISPUTED, SECTION_REASSIGNED, SECTION_REJECTED)
OWNER_CELL_EMPTY = (SECTION_REASSIGNED, SECTION_REJECTED)
NEED_OWNER = "выберите ответственного"
WAITS_EXECUTOR = "задача ждёт исполнителя — вынесите на планёрку или отклоните"
SKIPPED_EXECUTOR = "шаг исполнителя пропущен: решение владельца реестра"
EDIT_PENDING = TaskStatus.CANDIDATE.value  # статус TaskEdit при разборе записи
EDIT_SENT = "передана владельцу"  # TaskEdit ушла в файле владельца; применяется вручную
CHOOSE_OWNER_PREFIX = "выбрать ответственного"
COMMENT_PREFIX = "комментарий владельца реестра: "
REGISTRIES = ["общий", "закрытый"]
PRIORITIES = ["A", "B", "C"]
REGULATION = "регламент"

TASKS_SHEET = "Задачи"
HOWTO_SHEET = "Как заполнить"
EMPLOYEES_SHEET = "Справочник"
EDITS_SHEET = "Правки к задачам"
EDITS_HEADERS = ["Номер", "Что меняется", "Цитата", "Таймкод"]
EDITS_WIDTHS = [9, 50, 50, 10]
HEADERS = ["Номер", "Задача", "Ответственный", "Срок", "Раздел", "Вопросы", "Реестр",
           "Приоритет", "Решение", "Комментарий"]
WIDTHS = [9, 40, 28, 12, 26, 40, 12, 10, 14, 30]
WRAP_COLS = {2, 5, 6, 10}
DATE_FORMAT = "DD.MM.YYYY"
HOWTO = [
    "В колонке «Решение» выберите для каждой строки: «утверждаю» или «отклоняю». "
    "Пустое «Решение» — задача остаётся как есть и придёт в следующем файле.",
    "«Утверждаю»: задача уходит в работу. Проверьте «Задача», «Срок» (дд.мм.гггг), "
    "«Ответственный», «Реестр» (общий или закрытый) и «Приоритет» (A, B, C). "
    "Исправленные «Задача» и «Срок» применяются. Для регламента приоритет оставьте пустым.",
    "Разделы «спорная: …», «переназначена», «отклонена исполнителем»: если утверждаете, "
    "выберите ответственного из списка. Без него файл не примут.",
    "«Не вернулся файл»: исполнитель не ответил в срок. Утвердить нельзя — вынесите на "
    "планёрку или отклоните. Можно оставить пустым.",
    "«Реестр» заполнен предложением системы. Закрытым задачу делаете вы: решите сами.",
    "Колонка «Вопросы» — что осталось неясным по задаче.",
    "Лист «Правки к задачам» (если есть) — для сведения: изменения к задачам из записи встречи. "
    "Система их не применяет, внесите вручную.",
    "Верните файл ответом на письмо, в формате .xlsx. Строки и листы не добавляйте и не удаляйте.",
]


class OwnerDecision(str, Enum):
    APPROVE = "утверждаю"
    REJECT = "отклоняю"


def normalize_owner_decision(text: str) -> OwnerDecision | None:
    key = norm_name(text or "")
    for d in OwnerDecision:
        if norm_name(d.value) == key:
            return d
    return None


@dataclass
class OwnerRow:
    number: int
    decision: OwnerDecision
    owner_name: str | None
    registry: str
    priority: str | None
    comment: str | None
    row: int = 0  # номер строки в Excel, для сообщений об ошибках
    section: str | None = None
    title: str | None = None  # «Задача»: пусто — без изменений
    due_date: date | None = None  # «Срок»: пусто — без изменений


@dataclass
class ParsedOwnerReview:
    review_id: int
    rows: list[OwnerRow]


def _section(task: Task, not_returned: set[int]) -> str | None:
    """Первый подходящий раздел; None — владельцу решать нечего."""
    if task.status == TaskStatus.CONFIRMED_BY_EXECUTOR.value:
        return SECTION_CONFIRMED
    if task.status != TaskStatus.CANDIDATE.value:
        return None
    if task.owner_id is None:
        if any(q.startswith(CHOOSE_OWNER_PREFIX) for q in task.questions or []):
            return SECTION_AMBIGUOUS
        return SECTION_NO_OWNER
    if FLAG_REASSIGNED in task.flags:
        return SECTION_REASSIGNED
    if FLAG_REJECTED in task.flags:
        return SECTION_REJECTED
    if task.number in not_returned:
        return SECTION_NOT_RETURNED
    return None


def _not_returned(session: Session) -> set[int]:
    """Номера из файлов исполнителей, которые эскалированы и не вернулись."""
    numbers: set[int] = set()
    for rf in session.scalars(select(ReviewFile).where(
            ReviewFile.kind == "executor", ReviewFile.escalated_at.is_not(None),
            ReviewFile.returned_at.is_(None))):
        numbers.update(rf.task_numbers)
    return numbers


def _approve_problem(section: str | None, owner_chosen: bool) -> str | None:
    """Почему «утверждаю» нельзя принять для раздела; None — можно."""
    if section in (None, SECTION_NOT_RETURNED):
        return WAITS_EXECUTOR
    if section in CHOOSE_OWNER and not owner_chosen:
        return NEED_OWNER
    return None


def pending_task_edits(session: Session) -> list[TaskEdit]:
    """Правки к задачам из записей, ещё не переданные владельцу реестра."""
    return list(session.scalars(select(TaskEdit).where(TaskEdit.status == EDIT_PENDING)
                                .order_by(TaskEdit.task_number, TaskEdit.id)))


def pending_owner_items(session: Session) -> list[tuple[Task, str]]:
    """Задачи, которые ждут решения владельца реестра, с разделом; по разделам и номерам."""
    not_returned = _not_returned(session)
    pending = [TaskStatus.CANDIDATE.value, TaskStatus.CONFIRMED_BY_EXECUTOR.value]
    items: list[tuple[int, Task, str]] = []
    for t in session.scalars(select(Task).where(Task.status.in_(pending)).order_by(Task.number)):
        section = _section(t, not_returned)
        if section is not None:
            items.append((SECTIONS.index(section), t, section))
    items.sort(key=lambda i: (i[0], i[1].number))
    return [(t, section) for _, t, section in items]


def build_owner_file(session: Session, owner: Employee, out_dir: Path,
                     now: datetime) -> ReviewFile | None:
    """Файл владельцу: всё, что ждёт его решения, и непереданные правки к задачам (для
    сведения). Нет ни того, ни другого — None. Статус правок меняет вызывающий после отправки."""
    items = pending_owner_items(session)
    edits = pending_task_edits(session)
    if not items and not edits:
        return None

    review = ReviewFile(kind="owner", employee_id=owner.id, path="",
                        task_numbers=sorted(t.number for t, _ in items))
    session.add(review)
    session.flush()

    names, _ = employee_index(session)
    wb = Workbook()
    ws = wb.active
    ws.title = TASKS_SHEET
    ws.append(HEADERS)
    for t, section in items:
        owner_cell = None if section in OWNER_CELL_EMPTY else names.get(t.owner_id)
        ws.append([format_number(t.number), t.title, owner_cell, t.due_date, section,
                   "\n".join(t.questions or []), t.registry_suggested or t.registry, t.priority])
    last = len(items) + 1
    for r in range(2, last + 1):
        ws.cell(r, 4).number_format = DATE_FORMAT
    style_sheet(ws, WIDTHS, WRAP_COLS)
    if items:  # файл может состоять из одних правок к задачам
        add_list_validation(ws, f"G2:G{last}", REGISTRIES)
        add_list_validation(ws, f"H2:H{last}", PRIORITIES)
        add_list_validation(ws, f"I2:I{last}", [d.value for d in OwnerDecision])
        add_employee_dropdown(wb, ws, session, f"C2:C{last}", EMPLOYEES_SHEET)
    if edits:
        es = wb.create_sheet(EDITS_SHEET)
        es.append(EDITS_HEADERS)
        for e in edits:
            es.append([format_number(e.task_number), e.change, e.quote, e.timecode])
        style_sheet(es, EDITS_WIDTHS, {2, 3})

    how = wb.create_sheet(HOWTO_SHEET)
    how.column_dimensions["A"].width = 110
    for i, line in enumerate(HOWTO, start=1):
        how.append([f"{i}. {line}"])
    write_meta(wb, review.id, review.task_numbers)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"реестр_{review.id}.xlsx"
    wb.save(path)
    review.path = str(path)
    session.flush()
    return review


def _priority(text: str | None) -> str | None:
    """A/B/C; кириллические А, В, С — те же буквы. Иное — ValueError."""
    if text is None:
        return None
    key = text.strip().upper().translate(str.maketrans("АВС", "ABC"))
    if key not in PRIORITIES:
        raise ValueError(text)
    return key


def parse_owner_file(path: Path) -> ParsedOwnerReview | ReviewErrors:
    """Разбор возвращённого файла. На ошибки пользователя не бросает: ReviewErrors."""
    try:
        review_id, allowed = read_meta(path)
        wb = load_workbook(path, data_only=True)
        ws = wb[TASKS_SHEET]
        header = {cell_text(c.value): i for i, c in enumerate(ws[1])}
        missing = [h for h in HEADERS if h not in header]
        if missing:
            return ReviewErrors([f"лист «{TASKS_SHEET}»: нет колонки «{missing[0]}»"])
        data = [(r, [c.value for c in row]) for r, row in
                enumerate(ws.iter_rows(min_row=2), start=2)]
    except Exception:  # не xlsx, битый файл, нет листов — человеку один ответ
        return ReviewErrors([NOT_FROM_SYSTEM])

    def get(vals: list, name: str):
        i = header[name]
        return vals[i] if i < len(vals) else None

    errors: list[str] = []
    rows: list[OwnerRow] = []
    seen: set[int] = set()
    for r, vals in data:
        raw_number = get(vals, "Номер")
        if cell_text(raw_number) is None:
            continue
        try:
            number = cell_number(raw_number)
        except ValueError:
            errors.append(f"строка {r}: не удалось прочитать номер «{cell_text(raw_number)}»")
            continue
        if number not in allowed:
            errors.append(f"строка {r}: задачи {format_number(number)} нет в этом файле")
            continue
        if number in seen:
            errors.append(f"строка {r}: номер {format_number(number)} повторяется")
            continue
        seen.add(number)

        raw_decision = cell_text(get(vals, "Решение"))
        if raw_decision is None:
            continue  # пустое решение: строка остаётся как есть (F5)
        decision = normalize_owner_decision(raw_decision)
        if decision is None:
            errors.append(f"строка {r}: решение «{raw_decision}» не из списка "
                          "(утверждаю, отклоняю)")
            continue
        owner = cell_text(get(vals, "Ответственный"))
        section = cell_text(get(vals, "Раздел"))
        registry = norm_name(cell_text(get(vals, "Реестр")) or "")
        raw_priority = cell_text(get(vals, "Приоритет"))
        comment = cell_text(get(vals, "Комментарий"))
        if decision is OwnerDecision.REJECT:
            rows.append(OwnerRow(number, decision, None, "", None, comment, r, section))
            continue
        problems = []
        # Ячейка «Раздел» — подсказка для быстрого ответа; окончательно решает приём по БД.
        approve_problem = _approve_problem(section, owner is not None) if section in SECTIONS \
            else None
        if approve_problem:
            problems.append(f"строка {r}: {approve_problem}")
        try:
            due = parse_date(get(vals, "Срок"))
        except ValueError:
            problems.append(f"строка {r}: срок «{cell_text(get(vals, 'Срок'))}» не распознан, "
                            "нужен формат дд.мм.гггг")
        if registry not in REGISTRIES:
            problems.append(f"строка {r}: реестр «{registry or '—'}» не из списка "
                            "(общий, закрытый)")
        try:
            priority = _priority(raw_priority)
        except ValueError:
            problems.append(f"строка {r}: приоритет «{raw_priority}» не из списка (A, B, C)")
        if problems:
            errors.extend(problems)
            continue
        rows.append(OwnerRow(number, decision, owner, registry, priority, comment, r, section,
                             cell_text(get(vals, "Задача")), due))
    if errors:
        return ReviewErrors(errors)
    return ParsedOwnerReview(review_id, rows)


def apply_owner_review(session: Session, review: ReviewFile, parsed: ParsedOwnerReview, *,
                       now: datetime | None = None) -> ApplyReport:
    """Применяет решения владельца реестра. Любая ошибка — ReviewRejected, ничего не меняется."""
    now = now or datetime.now(timezone.utc)
    if parsed.review_id != review.id:
        raise ReviewRejected(ReviewErrors([NOT_FROM_SYSTEM]))
    names, by_name = employee_index(session)
    not_returned = _not_returned(session)
    errors: list[str] = []
    owners: dict[int, int] = {}
    pending = {TaskStatus.CANDIDATE.value, TaskStatus.CONFIRMED_BY_EXECUTOR.value}
    tasks: dict[int, Task] = {}
    for row in parsed.rows:
        if row.number not in review.task_numbers:
            errors.append(f"строка {row.row}: задачи {format_number(row.number)} нет в этом файле")
            continue
        task = session.scalar(select(Task).where(Task.number == row.number))
        if task is None or task.status not in pending:
            continue  # решение уже принято
        tasks[row.number] = task
        if row.decision is OwnerDecision.REJECT:
            continue
        if row.owner_name:
            key = norm_name(row.owner_name)
            if key not in by_name:
                errors.append(f"строка {row.row}: ответственный не найден в справочнике")
            elif by_name[key] is None:
                errors.append(f"строка {row.row}: ответственный указан неоднозначно в справочнике")
            else:
                owners[row.number] = by_name[key]
        problem = _approve_problem(_section(task, not_returned), row.number in owners)
        if problem is None and task.owner_id is None and row.number not in owners:
            problem = NEED_OWNER
        if problem:
            errors.append(f"строка {row.row}: {problem}")
        if task.type == REGULATION and row.priority:
            errors.append(f"строка {row.row}: у регламента приоритет не ставится, очистите колонку")
    if errors:
        raise ReviewRejected(ReviewErrors(errors))

    actor = f"владелец реестра:{review.employee_id}"
    report = ApplyReport()
    for row in parsed.rows:
        task = tasks.get(row.number)
        if task is None or task.status not in pending:
            report.skipped += 1
            continue
        if row.comment:
            _add_unique(task.questions, COMMENT_PREFIX + row.comment)
        if row.decision is OwnerDecision.REJECT:
            transition(session, task, TaskStatus.REJECTED, actor)
            report.rejected += 1
            continue
        new_owner = owners.get(row.number)
        if new_owner is not None and new_owner != task.owner_id:
            session.add(History(task_id=task.id, event="правка", actor=actor,
                                old=f"ответственный: {names.get(task.owner_id, '—')}",
                                new=f"ответственный: {names[new_owner]}"))
            task.owner_id = new_owner
        if row.title:
            _edit(session, task, "название", "title", row.title, actor)
        if row.due_date:
            _edit(session, task, "срок", "due_date", row.due_date, actor)
        _edit(session, task, "реестр", "registry", row.registry, actor)
        _edit(session, task, "приоритет", "priority", row.priority, actor)
        if task.status == TaskStatus.CANDIDATE.value:
            session.add(History(task_id=task.id, event="примечание", new=SKIPPED_EXECUTOR,
                                actor=actor))
        transition(session, task, TaskStatus.IN_PROGRESS, actor)
        report.approved += 1
    if review.returned_at is None:
        review.returned_at = now
    session.flush()
    return report
