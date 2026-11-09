"""Импорт реестра пилота из xlsx: номера И-NNN сохраняются, нумерация продолжается.

Повторный импорт обновляет задачи реестра; задачи из записей (source_id) не трогает.
"""
import tomllib
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.core.directory import ImportReport, load_directory, norm_name
from planerka.core.numbering import parse_number, set_sequence_after
from planerka.core.statuses import TaskStatus
from planerka.db.models import History, Task
from planerka.settings import PROJECT_ROOT

MAPPING_PATH = PROJECT_ROOT / "config" / "registry_mapping.toml"
Q_OWNER_MISSING = "ответственный не найден в справочнике"
Q_OWNER_AMBIGUOUS = "ответственный неоднозначен: выбрать при утверждении"
Q_TYPE = "тип не указан: уточнить"
Q_STATUS = "статус не распознан: уточнить"


class RegistryMapping(BaseModel):
    """Названия колонок реестра."""
    number: str
    title: str
    type: str
    priority: str
    goal_2027: str
    owner: str
    due_date: str
    control_date: str
    done_criteria: str
    status: str

    @classmethod
    def load(cls, path: Path = MAPPING_PATH) -> "RegistryMapping":
        with open(path, "rb") as f:
            return cls(**tomllib.load(f))


def _text(row: tuple, idx: dict[str, int], col: str) -> str | None:
    i = idx.get(col)
    if i is None or i >= len(row) or row[i] is None:
        return None
    return str(row[i]).strip() or None


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_number(value) -> int | None:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value).strip()
    try:
        return int(s) if s.isdigit() else parse_number(s)
    except ValueError:
        return None


def import_registry(session: Session, path: Path, mapping: RegistryMapping,
                    registry: str = "общий") -> ImportReport:
    report = ImportReport()
    with closing(load_workbook(path, read_only=True, data_only=True)) as wb:
        rows = wb.active.iter_rows(values_only=True)
        header = next(rows, ())
        idx = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
        missing = [c for c in (mapping.number, mapping.title) if c not in idx]
        if missing:
            report.problems.append("нет обязательных колонок реестра")
            return report
        data = list(rows)

    by_variant: dict[str, set[int]] = {}
    for emp in load_directory(session):
        for v in emp.variants:
            by_variant.setdefault(norm_name(v), set()).add(emp.id)
    statuses = {norm_name(s.value): s for s in TaskStatus}

    max_number = 0
    for n, row in enumerate(data, start=2):
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        raw_number = row[idx[mapping.number]] if idx[mapping.number] < len(row) else None
        number = _parse_number(raw_number) if raw_number is not None else None
        if number is None:
            report.problems.append(f"строка {n}: не удалось разобрать номер")
            continue
        title = _text(row, idx, mapping.title)
        if not title:
            report.problems.append(f"строка {n}: нет названия задачи")
            continue

        questions: list[str] = []
        owner_cell = _text(row, idx, mapping.owner)
        owner_id = None
        if owner_cell:
            found = by_variant.get(norm_name(owner_cell), set())
            if len(found) == 1:
                owner_id = next(iter(found))
            else:
                questions.append(Q_OWNER_AMBIGUOUS if found else Q_OWNER_MISSING)
        else:
            questions.append(Q_OWNER_MISSING)

        status_cell = _text(row, idx, mapping.status)
        status = statuses.get(norm_name(status_cell)) if status_cell else None
        if status is None:
            status = TaskStatus.CANDIDATE
            questions.append(Q_STATUS)

        dates = {}
        for attr, col in (("due_date", mapping.due_date), ("control_date", mapping.control_date)):
            i = idx.get(col)
            value = row[i] if i is not None and i < len(row) else None
            if value is None or str(value).strip() == "":
                dates[attr] = None
            else:
                dates[attr] = _parse_date(value)
                if dates[attr] is None:
                    questions.append(f"не удалось разобрать дату: {col}")

        task = session.scalars(select(Task).where(Task.number == number)).first()
        if task is not None and task.source_id is not None:  # задачу ведёт система (F4)
            report.problems.append(f"строка {n}: номер занят задачей из записи")
            continue
        is_new = task is None
        if is_new:
            task = Task(number=number)
            session.add(task)
            report.created += 1
        else:
            report.updated += 1
        old_status = None if is_new else task.status
        task.title = title
        task.type = _text(row, idx, mapping.type)
        if task.type is None:
            questions.append(Q_TYPE)
        task.priority = _text(row, idx, mapping.priority)
        task.goal_2027 = _text(row, idx, mapping.goal_2027)
        task.owner_id = owner_id
        task.due_date = dates["due_date"]
        task.control_date = dates["control_date"]
        task.done_criteria = _text(row, idx, mapping.done_criteria)
        task.status = status.value
        task.registry = registry
        task.questions = questions
        session.flush()
        session.add(History(task_id=task.id, event="импорт", old=old_status,
                            new=status.value, actor="import"))
        max_number = max(max_number, number)

    if max_number:
        set_sequence_after(session, max_number)
    session.flush()
    return report
