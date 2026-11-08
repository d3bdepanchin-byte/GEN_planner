"""Файл исполнителю: свои кандидаты, решение, правка; приём ответа (Д-72, Д-74)."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.core.directory import norm_name
from planerka.core.numbering import format_number, parse_number
from planerka.core.statuses import TaskStatus, transition
from planerka.db.models import Employee, History, ReviewFile, Task
from planerka.review.xlsx import (
    NOT_FROM_SYSTEM, cell_text, parse_date, read_meta, style_sheet, write_meta,
)

__all__ = ["read_meta", "ReviewDecision", "normalize_decision", "build_executor_file",
           "ParsedRow", "ParsedReview", "ReviewErrors", "ReviewRejected", "ApplyReport",
           "parse_executor_file", "apply_executor_review"]

FLAG_REASSIGNED = "переназначена"
FLAG_REJECTED = "отклонена исполнителем"
COMMENT_PREFIX = "комментарий исполнителя: "
TASKS_SHEET = "Задачи"
HOWTO_SHEET = "Как заполнить"
EMPLOYEES_SHEET = "Справочник"

HEADERS = ["Номер", "Задача", "Тип", "Приоритет", "Срок", "Контрольная дата",
           "Критерий готовности", "Цитата", "Вопросы", "Решение",
           "Правка: задача", "Правка: срок", "Правка: ответственный", "Комментарий"]
WIDTHS = [9, 40, 14, 10, 12, 14, 30, 45, 35, 16, 30, 14, 28, 30]
WRAP_COLS = {2, 7, 8, 9, 11, 13, 14}
DATE_COLS = (5, 6, 12)
DATE_FORMAT = "DD.MM.YYYY"
HOWTO = [
    "В колонке «Решение» выберите для каждой строки: «подтверждаю», «правка» или «отклоняю».",
    "«Правка»: впишите новое значение в «Правка: задача», «Правка: срок» (дд.мм.гггг) "
    "или «Правка: ответственный» (выбор из списка). Заполняйте только то, что меняется.",
    "Задача должна уйти другому человеку: выберите «правка» и укажите его в «Правка: ответственный». "
    "Решение примет владелец реестра.",
    "Верните файл ответом на письмо, в формате .xlsx. Строки и листы не добавляйте и не удаляйте.",
]


class ReviewDecision(str, Enum):
    CONFIRM = "подтверждаю"
    EDIT = "правка"
    REJECT = "отклоняю"


def normalize_decision(text: str) -> ReviewDecision | None:
    key = norm_name(text or "")
    for d in ReviewDecision:
        if norm_name(d.value) == key:
            return d
    return None


@dataclass
class ParsedRow:
    number: int
    decision: ReviewDecision
    title: str | None = None
    due_date: date | None = None
    owner_name: str | None = None
    comment: str | None = None
    row: int = 0  # номер строки в Excel, для сообщений об ошибках


@dataclass
class ParsedReview:
    review_id: int
    rows: list[ParsedRow]


@dataclass
class ReviewErrors:
    lines: list[str]


@dataclass
class ApplyReport:
    confirmed: int = 0
    to_owner: int = 0
    skipped: int = 0  # строки, по которым решение уже применено (повторный возврат файла)


class ReviewRejected(Exception):
    """Файл разобран, но не применён: строки ошибок уходят отправителю."""

    def __init__(self, errors: ReviewErrors):
        super().__init__("; ".join(errors.lines))
        self.errors = errors


def build_executor_file(session: Session, employee: Employee, tasks: list[Task],
                        out_dir: Path) -> ReviewFile:
    if any(t.owner_id != employee.id for t in tasks):
        raise ValueError("в файл исполнителя попадают только его кандидаты")
    review = ReviewFile(kind="executor", employee_id=employee.id, path="",
                        task_numbers=[t.number for t in tasks])
    session.add(review)
    session.flush()

    wb = Workbook()
    ws = wb.active
    ws.title = TASKS_SHEET
    ws.append(HEADERS)
    for t in tasks:
        ws.append([format_number(t.number), t.title, t.type, t.priority, t.due_date,
                   t.control_date, t.done_criteria, t.quote, "\n".join(t.questions or [])])
    last = len(tasks) + 1
    for r in range(2, last + 1):
        for c in DATE_COLS:
            ws.cell(r, c).number_format = DATE_FORMAT
    style_sheet(ws, WIDTHS, WRAP_COLS)

    decisions = DataValidation(
        type="list", formula1='"' + ",".join(d.value for d in ReviewDecision) + '"',
        allow_blank=True)
    decisions.add(f"J2:J{last}")
    ws.add_data_validation(decisions)

    names = list(session.scalars(select(Employee.full_name).order_by(Employee.full_name)))
    lst = wb.create_sheet(EMPLOYEES_SHEET)
    for n in names:
        lst.append([n])
    lst.sheet_state = "hidden"
    if names:
        owners = DataValidation(
            type="list", formula1=f"='{EMPLOYEES_SHEET}'!$A$1:$A${len(names)}", allow_blank=True)
        owners.showErrorMessage = False  # имя вне списка отсечёт приём файла
        owners.add(f"M2:M{last}")
        ws.add_data_validation(owners)

    how = wb.create_sheet(HOWTO_SHEET)
    how.column_dimensions["A"].width = 110
    for i, line in enumerate(HOWTO, start=1):
        how.append([f"{i}. {line}"])
    write_meta(wb, review.id, review.task_numbers)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"уточнение_{employee.id}_{review.id}.xlsx"
    wb.save(path)
    review.path = str(path)
    session.flush()
    return review


def _number(value) -> int | None:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = cell_text(value)
    if text is None:
        return None
    return int(text) if text.isdigit() else parse_number(text)


def parse_executor_file(path: Path) -> ParsedReview | ReviewErrors:
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
    rows: list[ParsedRow] = []
    seen: set[int] = set()
    for r, vals in data:
        raw_number = get(vals, "Номер")
        if cell_text(raw_number) is None:
            continue
        try:
            number = _number(raw_number)
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
            errors.append(f"строка {r}: не выбрано решение")
            continue
        decision = normalize_decision(raw_decision)
        if decision is None:
            errors.append(f"строка {r}: решение «{raw_decision}» не из списка "
                          "(подтверждаю, правка, отклоняю)")
            continue
        try:
            due = parse_date(get(vals, "Правка: срок"))
        except ValueError:
            errors.append(f"строка {r}: срок «{cell_text(get(vals, 'Правка: срок'))}» "
                          "не распознан, нужен формат дд.мм.гггг")
            continue
        title = cell_text(get(vals, "Правка: задача"))
        owner = cell_text(get(vals, "Правка: ответственный"))
        has_edit = bool(title or due or owner)
        if decision is ReviewDecision.EDIT and not has_edit:
            errors.append(f"строка {r}: выбрана правка, но поле «Правка: …» не заполнено")
            continue
        if decision is ReviewDecision.CONFIRM and has_edit:
            errors.append(f"строка {r}: заполнено поле «Правка: …», но выбрано «подтверждаю»; "
                          "выберите «правка»")
            continue
        if decision is ReviewDecision.REJECT:
            title = due = owner = None
        rows.append(ParsedRow(number, decision, title, due, owner,
                              cell_text(get(vals, "Комментарий")), r))
    if errors:
        return ReviewErrors(errors)
    return ParsedReview(review_id, rows)


def _edit(session: Session, task: Task, field: str, attr: str, new, actor: str) -> None:
    old = getattr(task, attr)
    if old == new:
        return
    setattr(task, attr, new)
    session.add(History(task_id=task.id, event="правка",
                        old=f"{field}: {old if old is not None else '—'}",
                        new=f"{field}: {new}", actor=actor))


def _add_unique(items: list[str], value: str) -> bool:
    if value in items:
        return False
    items.append(value)
    return True


def apply_executor_review(session: Session, review: ReviewFile, parsed: ParsedReview, *,
                          now: datetime | None = None) -> ApplyReport:
    """Применяет решения исполнителя. Ошибки в именах — ReviewRejected, ничего не меняется."""
    now = now or datetime.now(timezone.utc)
    if parsed.review_id != review.id:
        raise ReviewRejected(ReviewErrors([NOT_FROM_SYSTEM]))
    names = {e.id: e.full_name for e in session.scalars(select(Employee))}
    by_name: dict[str, int | None] = {}
    for eid, full_name in names.items():
        key = norm_name(full_name)
        by_name[key] = None if key in by_name else eid  # None — двое с одним именем
    errors: list[str] = []
    owners: dict[int, int] = {}
    for row in parsed.rows:
        if row.number not in review.task_numbers:
            errors.append(f"строка {row.row}: задачи {format_number(row.number)} нет в этом файле")
        elif row.owner_name:
            key = norm_name(row.owner_name)
            if key not in by_name:
                errors.append(f"строка {row.row}: ответственный не найден в справочнике")
            elif by_name[key] is None:
                errors.append(f"строка {row.row}: ответственный указан неоднозначно в справочнике")
            else:
                owners[row.number] = by_name[key]
    if errors:
        raise ReviewRejected(ReviewErrors(errors))

    actor = f"исполнитель:{review.employee_id}"
    report = ApplyReport()
    for row in parsed.rows:
        task = session.scalar(select(Task).where(Task.number == row.number))
        if task is None or task.status != TaskStatus.CANDIDATE.value:
            report.skipped += 1
            continue
        if row.decision is ReviewDecision.REJECT:
            if not _add_unique(task.flags, FLAG_REJECTED):
                report.skipped += 1
                continue
            report.to_owner += 1
        else:
            new_owner = owners.get(row.number)
            if new_owner is not None and new_owner != task.owner_id:
                # Условия передаваемой задачи решает владелец реестра: правки — только предложением.
                if not _add_unique(task.flags, FLAG_REASSIGNED):
                    report.skipped += 1
                    continue
                task.questions.append(f"исполнитель предлагает передать задачу: {names[new_owner]}")
                if row.due_date:
                    _add_unique(task.questions,
                                f"исполнитель предлагает срок: {row.due_date:%d.%m.%Y}")
                if row.title:
                    _add_unique(task.questions, f"исполнитель предлагает название: {row.title}")
                report.to_owner += 1
            else:
                if row.title:
                    _edit(session, task, "название", "title", row.title, actor)
                if row.due_date:
                    _edit(session, task, "срок", "due_date", row.due_date, actor)
                transition(session, task, TaskStatus.CONFIRMED_BY_EXECUTOR, actor)
                report.confirmed += 1
        if row.comment:
            _add_unique(task.questions, COMMENT_PREFIX + row.comment)
    if review.returned_at is None:
        review.returned_at = now
    session.flush()
    return report
