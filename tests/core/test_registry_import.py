from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from planerka.core.directory import import_employees
from planerka.core.numbering import next_task_number
from planerka.core.registry_import import RegistryMapping, import_registry
from planerka.db.models import History, Task

MAPPING = RegistryMapping.load(Path('config/registry_mapping.toml'))
HEADER = ['Номер', 'Задача', 'Тип', 'Приоритет', 'Цель 2027', 'Ответственный',
          'Срок', 'Дата контроля', 'Критерий выполнения', 'Статус']


def _book(path: Path, rows: list[list]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def directory(session, tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(['ФИО', 'Отдел', 'Должность', 'E-mail', 'Варианты имени'])
    ws.append(['Смирнов Артём Ильич', 'Снабжение', 'Начальник', 'smirnov@example.test', None])
    ws.append(['Иванова Ольга Сергеевна', 'Склад', 'Кладовщик', 'ivanova@example.test', None])
    ws.append(['Иванов Олег', 'Сервис', 'Мастер', 'ivanov@example.test', 'Иванов'])
    ws.append(['Иванов Игорь', 'Сервис', 'Мастер', 'ivanov2@example.test', 'Иванов'])
    p = tmp_path / 'hr.xlsx'
    wb.save(p)
    import_employees(session, p, {})


@pytest.fixture
def registry_xlsx(tmp_path):
    return _book(tmp_path / 'reg.xlsx', [
        ['И-011', 'Обновить прайс', 'поручение', 'высокий', 'Цель А', 'Смирнов Артем',
         date(2026, 12, 1), '15.12.2026', 'Прайс выложен', 'в работе'],
        [40, 'Закупить стеллажи', 'проект', 'обычный', None, 'Неизвестный Иван',
         None, None, None, 'выполнена'],
        ['И-072', 'Инвентаризация', 'регламент', None, None, 'Иванова Ольга',
         'завтра', None, None, 'непонятно'],
    ])


def _task(session, number):
    return session.scalars(select(Task).where(Task.number == number)).one()


def test_numbers_preserved_and_sequence_continues(session, directory, registry_xlsx, fresh_sequence):
    import_registry(session, registry_xlsx, MAPPING)
    assert {t.number for t in session.scalars(select(Task))} == {11, 40, 72}
    assert next_task_number(session) == 73


def test_unknown_owner_left_empty_with_question(session, directory, registry_xlsx):
    import_registry(session, registry_xlsx, MAPPING)
    t = _task(session, 40)
    assert t.owner_id is None
    assert any('не найден в справочнике' in q for q in t.questions)


def test_known_owner_matched_ignoring_yo_and_case(session, directory, registry_xlsx):
    import_registry(session, registry_xlsx, MAPPING)
    t = _task(session, 11)
    assert t.owner_id is not None and t.questions == []
    assert t.status == 'в работе'
    assert (t.due_date, t.control_date) == (date(2026, 12, 1), date(2026, 12, 15))


def test_ambiguous_owner_left_empty_with_question(session, directory, tmp_path):
    p = _book(tmp_path / 'a.xlsx', [[5, 'Задача', 'поручение', None, None, 'Иванов', None, None, None, 'в работе']])
    import_registry(session, p, MAPPING)
    t = _task(session, 5)
    assert t.owner_id is None
    assert 'ответственный неоднозначен: выбрать при утверждении' in t.questions


def test_unknown_status_and_bad_date_become_questions(session, directory, registry_xlsx):
    import_registry(session, registry_xlsx, MAPPING)
    t = _task(session, 72)
    assert t.status == 'на подтверждении'
    assert any('статус' in q for q in t.questions)
    assert t.due_date is None and any('дат' in q for q in t.questions)


def test_history_row_per_task(session, directory, registry_xlsx):
    import_registry(session, registry_xlsx, MAPPING)
    rows = session.scalars(select(History)).all()
    assert len(rows) == 3 and all(h.event == 'импорт' and h.actor == 'import' for h in rows)


def test_bad_number_row_skipped_with_problem_without_names(session, directory, tmp_path):
    p = _book(tmp_path / 'b.xlsx', [['abc', 'Секрет', 'поручение', None, None, None, None, None, None, None]])
    rep = import_registry(session, p, MAPPING)
    assert rep.created == 0
    assert rep.problems == ['строка 2: не удалось разобрать номер']


def test_closed_registry_file_marks_tasks(session, directory, registry_xlsx):
    import_registry(session, registry_xlsx, MAPPING, registry='закрытый')
    assert all(t.registry == 'закрытый' for t in session.scalars(select(Task)))


def test_reimport_is_idempotent(session, directory, registry_xlsx):
    import_registry(session, registry_xlsx, MAPPING)
    rep = import_registry(session, registry_xlsx, MAPPING)
    assert (rep.created, rep.updated) == (0, 3)
    assert len(session.scalars(select(Task)).all()) == 3


def test_empty_type_stays_empty_with_question(session, directory, tmp_path):
    p = _book(tmp_path / 't.xlsx', [[7, 'Задача', None, None, None, 'Иванова Ольга', None, None, None, 'в работе']])
    import_registry(session, p, MAPPING)
    t = _task(session, 7)
    assert t.type is None
    assert 'тип не указан: уточнить' in t.questions
