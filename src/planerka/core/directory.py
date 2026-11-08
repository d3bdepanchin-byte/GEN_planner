"""Справочник сотрудников: импорт из xlsx отдела кадров и варианты имён для маскирования."""
import tomllib
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planerka.db.models import ChannelId, Employee

DIMINUTIVES_PATH = Path("config/diminutives.toml")

_COL_NAME, _COL_DEPT, _COL_ROLE = "ФИО", "Отдел", "Должность"
_COL_EMAIL, _COL_VARIANTS = "E-mail", "Варианты имени"


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    problems: list[str] = field(default_factory=list)  # только номера строк, без ФИО


@dataclass(frozen=True)
class EmployeeRef:
    id: int
    full_name: str
    variants: list[str]


def norm_name(text: str) -> str:
    """Ключ сравнения имён: без регистра и без различия е/ё (HR пишет «Артем»)."""
    return " ".join(text.split()).casefold().replace("ё", "е")


def load_diminutives(path: Path = DIMINUTIVES_PATH) -> dict[str, list[str]]:
    with open(path, "rb") as f:
        return {k: list(v) for k, v in tomllib.load(f).items()}


def name_variants(full_name: str, diminutives: dict[str, list[str]]) -> list[str]:
    """Формы, в которых сотрудника могут назвать вслух. Порядок стабилен, без повторов."""
    parts = full_name.split()
    out = [" ".join(parts)]
    by_norm: dict[str, list[str]] = {}
    for key, forms in diminutives.items():
        by_norm.setdefault(norm_name(key), []).extend(forms)
    if len(parts) >= 2:
        surname, first = parts[0], parts[1]
        out += [f"{surname} {first}", f"{first} {surname}", surname]
        if len(parts) >= 3:
            out.append(f"{first} {parts[2]}")
        out.append(first)
        for dim in by_norm.get(norm_name(first), []):
            out += [dim, f"{dim} {surname}"]
    return list(dict.fromkeys(v for v in out if v))


def _cell(row: tuple, idx: dict[str, int], col: str) -> str | None:
    i = idx.get(col)
    if i is None or i >= len(row) or row[i] is None:
        return None
    return str(row[i]).strip() or None


def import_employees(session: Session, path: Path,
                     diminutives: dict[str, list[str]]) -> ImportReport:
    """Ключ повторного импорта: e-mail (без регистра), без него — ФИО."""
    report = ImportReport()
    with closing(load_workbook(path, read_only=True, data_only=True)) as wb:
        _import_rows(session, wb, diminutives, report)
    return report


def _import_rows(session: Session, wb, diminutives: dict[str, list[str]],
                 report: ImportReport) -> None:
    rows = wb.active.iter_rows(values_only=True)
    header = next(rows, ())
    idx = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
    if _COL_NAME not in idx:
        report.problems.append(f"нет колонки «{_COL_NAME}»")
        return

    for n, row in enumerate(rows, start=2):
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        full_name = _cell(row, idx, _COL_NAME)
        if not full_name:
            report.problems.append(f"строка {n}: нет ФИО")
            continue
        full_name = " ".join(full_name.split())
        email = _cell(row, idx, _COL_EMAIL)
        email = email.lower() if email else None
        if not email:
            report.problems.append(f"строка {n}: нет e-mail")

        emp = None
        if email:
            emp = session.scalars(select(Employee).where(
                func.lower(Employee.email) == email)).first()
        if emp is None:
            by_name = select(Employee).where(Employee.full_name == full_name)
            if email:  # другой адрес — другой человек; сливаем только с записью без адреса
                by_name = by_name.where(Employee.email.is_(None))
            emp = session.scalars(by_name).first()
        if emp is None:
            emp = Employee(full_name=full_name)
            session.add(emp)
            report.created += 1
        else:
            report.updated += 1

        extra = _cell(row, idx, _COL_VARIANTS)
        hr_variants = [v.strip() for v in extra.split(",")] if extra else []
        emp.full_name = full_name
        emp.department = _cell(row, idx, _COL_DEPT)
        emp.role = _cell(row, idx, _COL_ROLE)
        if email:
            emp.email = email
        emp.name_variants = list(dict.fromkeys(
            name_variants(full_name, diminutives) + [v for v in hr_variants if v]))
        session.flush()

        if email:
            ch = session.scalars(select(ChannelId).where(
                ChannelId.channel == "email", ChannelId.external_id == email)).first()
            if ch is None:
                session.add(ChannelId(employee_id=emp.id, channel="email", external_id=email))
                session.flush()
            elif ch.employee_id != emp.id:
                report.problems.append(f"строка {n}: e-mail занят другим сотрудником")


def load_directory(session: Session) -> list[EmployeeRef]:
    return [EmployeeRef(e.id, e.full_name, list(e.name_variants or []))
            for e in session.scalars(select(Employee).order_by(Employee.id))]
