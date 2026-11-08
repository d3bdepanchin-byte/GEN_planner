"""Общее для файлов на уточнение: оформление, скрытый лист meta, разбор ячеек."""
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.core.directory import norm_name
from planerka.core.numbering import parse_number
from planerka.db.models import Employee

META_SHEET = "meta"
NOT_FROM_SYSTEM = "файл не из системы или повреждён"
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


class MetaError(ValueError):
    """В файле нет валидного листа meta."""


def style_sheet(ws: Worksheet, widths: list[int], wrap_cols: set[int]) -> None:
    """Жирная закреплённая шапка, ширины колонок, перенос текста в длинных колонках."""
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=cell.column in wrap_cols, vertical="top")
    ws.freeze_panes = "A2"


def write_meta(wb: Workbook, review_id: int, numbers: list[int]) -> None:
    ws = wb.create_sheet(META_SHEET)
    ws["A1"], ws["B1"] = "review_id", review_id
    ws["A2"], ws["B2"] = "numbers", ",".join(str(n) for n in sorted(numbers))
    ws.sheet_state = "hidden"


def read_meta(path: Path) -> tuple[int, list[int]]:
    """id ReviewFile и номера задач из скрытого листа meta. Иначе MetaError."""
    try:
        wb = load_workbook(path, read_only=True)
        try:
            ws = wb[META_SHEET]
            rid, raw = ws["B1"].value, ws["B2"].value
        finally:
            wb.close()
        if isinstance(rid, bool) or not isinstance(rid, int):
            raise ValueError("review_id")
        numbers = [int(x) for x in str(raw).split(",") if x.strip()]
    except Exception as exc:  # битый zip, нет листа, чужие значения — для человека всё одно
        raise MetaError(NOT_FROM_SYSTEM) from exc
    return rid, sorted(numbers)


def cell_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_date(value) -> date | None:
    """Ячейка даты или текст дд.мм.гггг (и д.м.гггг). Пусто -> None, иначе ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = cell_text(value)
    if text is None:
        return None
    m = _DATE_RE.match(text)
    if m is None:
        raise ValueError(text)
    d, mo, y = (int(g) for g in m.groups())
    return date(y, mo, d)


def cell_number(value) -> int | None:
    """Номер задачи: И-007, 7 или 7.0. Пусто -> None, нечитаемое -> ValueError."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = cell_text(value)
    if text is None:
        return None
    return int(text) if text.isdigit() else parse_number(text)


def employee_index(session: Session) -> tuple[dict[int, str], dict[str, int | None]]:
    """id -> ФИО и ключ имени -> id (None, если одно имя у двоих)."""
    names = {e.id: e.full_name for e in session.scalars(select(Employee))}
    by_name: dict[str, int | None] = {}
    for eid, full_name in names.items():
        key = norm_name(full_name)
        by_name[key] = None if key in by_name else eid
    return names, by_name


def add_list_validation(ws: Worksheet, cells: str, options: list[str]) -> None:
    dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True)
    dv.add(cells)
    ws.add_data_validation(dv)


def add_employee_dropdown(wb: Workbook, ws: Worksheet, session: Session, cells: str,
                          sheet_name: str) -> None:
    """Скрытый лист со всеми ФИО и выпадающий список по нему; имя вне списка отсечёт приём."""
    names = list(session.scalars(select(Employee.full_name).order_by(Employee.full_name)))
    lst = wb.create_sheet(sheet_name)
    for n in names:
        lst.append([n])
    lst.sheet_state = "hidden"
    if names:
        dv = DataValidation(
            type="list", formula1=f"='{sheet_name}'!$A$1:$A${len(names)}", allow_blank=True)
        dv.showErrorMessage = False
        dv.add(cells)
        ws.add_data_validation(dv)
