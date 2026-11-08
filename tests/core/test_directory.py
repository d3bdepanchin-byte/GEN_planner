from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from planerka.core.directory import (
    EmployeeRef,
    import_employees,
    load_diminutives,
    load_directory,
    name_variants,
)
from planerka.db.models import ChannelId, Employee

DIM = {'Александр': ['Саша', 'Шура'], 'Ольга': ['Оля']}
HEADER = ['ФИО', 'Отдел', 'Должность', 'E-mail', 'Варианты имени']


def _xlsx(path: Path, rows: list[list]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def employees_xlsx(tmp_path):
    return _xlsx(tmp_path / 'hr.xlsx', [
        ['Смирнов Александр Петрович', 'Снабжение', 'Начальник', 'Smirnov@example.test', 'Петрович'],
        ['Иванова Ольга Сергеевна', 'Склад', 'Кладовщик', 'ivanova@example.test', None],
        ['Кузнецов Игорь', 'Сервис', 'Мастер', 'kuznetsov@example.test', 'Кузя, Игорь К.'],
    ])


@pytest.fixture
def employees_xlsx_no_email(tmp_path):
    return _xlsx(tmp_path / 'hr_no_email.xlsx', [
        ['Смирнов Александр Петрович', 'Снабжение', 'Начальник', 'smirnov@example.test', None],
        ['Иванова Ольга Сергеевна', 'Склад', 'Кладовщик', None, None],
    ])


def test_variants_cover_orders_and_diminutives():
    v = name_variants('Смирнов Александр Петрович', DIM)
    for x in ['Смирнов Александр', 'Александр Смирнов', 'Смирнов',
              'Александр Петрович', 'Саша', 'Саша Смирнов']:
        assert x in v


def test_variants_full_name_bare_first_name_and_no_duplicates():
    v = name_variants('Смирнов Александр Петрович', DIM)
    assert v[0] == 'Смирнов Александр Петрович'
    assert 'Александр' in v and 'Шура Смирнов' in v
    assert len(v) == len(set(v))


def test_variants_without_patronymic_and_single_word():
    assert 'Оля Иванова' in name_variants('Иванова Ольга', DIM)
    assert name_variants('Мадонна', DIM) == ['Мадонна']


def test_import_creates_employee_and_email_channel(session, employees_xlsx):
    rep = import_employees(session, employees_xlsx, DIM)
    assert rep.created == 3
    e = session.scalars(select(Employee).where(Employee.email == 'smirnov@example.test')).one()
    ch = session.scalars(select(ChannelId).where(ChannelId.employee_id == e.id)).one()
    assert ch.channel == 'email'
    assert ch.external_id == 'smirnov@example.test'
    assert (e.department, e.role) == ('Снабжение', 'Начальник')


def test_hr_variants_column_added(session, employees_xlsx):
    import_employees(session, employees_xlsx, DIM)
    e = session.scalars(select(Employee).where(Employee.email == 'kuznetsov@example.test')).one()
    assert 'Кузя' in e.name_variants and 'Игорь К.' in e.name_variants


def test_reimport_updates_not_duplicates(session, employees_xlsx):
    import_employees(session, employees_xlsx, DIM)
    rep = import_employees(session, employees_xlsx, DIM)
    assert (rep.created, rep.updated) == (0, 3)
    assert len(session.scalars(select(Employee)).all()) == 3
    assert len(session.scalars(select(ChannelId)).all()) == 3


def test_reimport_matches_email_case_insensitive_and_updates_fields(session, tmp_path, employees_xlsx):
    import_employees(session, employees_xlsx, DIM)
    p = _xlsx(tmp_path / 'hr2.xlsx', [
        ['Смирнов Александр Петрович', 'Закупки', 'Директор', 'SMIRNOV@example.test', None]])
    rep = import_employees(session, p, DIM)
    assert (rep.created, rep.updated) == (0, 1)
    e = session.scalars(select(Employee).where(Employee.email == 'smirnov@example.test')).one()
    assert e.department == 'Закупки'


def test_missing_email_reported_as_problem(session, employees_xlsx_no_email):
    rep = import_employees(session, employees_xlsx_no_email, DIM)
    assert any('e-mail' in p for p in rep.problems)
    assert rep.problems == ['строка 3: нет e-mail']  # номер строки, без ФИО
    assert rep.created == 2


def test_no_email_keyed_by_full_name(session, employees_xlsx_no_email):
    import_employees(session, employees_xlsx_no_email, DIM)
    rep = import_employees(session, employees_xlsx_no_email, DIM)
    assert (rep.created, rep.updated) == (0, 2)


def test_row_without_name_is_problem_not_import(session, tmp_path):
    p = _xlsx(tmp_path / 'x.xlsx', [[None, 'Склад', None, 'a@example.test', None]])
    rep = import_employees(session, p, DIM)
    assert rep.created == 0 and rep.problems == ['строка 2: нет ФИО']


def test_load_directory(session, employees_xlsx):
    import_employees(session, employees_xlsx, DIM)
    refs = load_directory(session)
    assert len(refs) == 3 and all(isinstance(r, EmployeeRef) for r in refs)
    r = next(r for r in refs if r.full_name.startswith('Иванова'))
    assert 'Оля' in r.variants


def test_load_diminutives_default_file():
    d = load_diminutives()
    assert 50 <= len(d) <= 80
    assert 'Санёк' in d['Александр'] and d['Ольга'][0] == 'Оля'


def test_diminutive_lookup_ignores_case_and_yo():
    dim = {'Артём': ['Тёма'], 'алёна': ['Алёнка']}
    v = name_variants('Смирнов Артем Ильич', dim)
    assert 'Тёма' in v and 'Тёма Смирнов' in v
    assert 'Алёнка' in name_variants('Орлова АЛЕНА', dim)


def test_email_owned_by_other_employee_is_problem_without_names(session, tmp_path):
    other = Employee(full_name='Петров Пётр')
    session.add(other)
    session.flush()
    session.add(ChannelId(employee_id=other.id, channel='email', external_id='busy@example.test'))
    session.flush()
    p = _xlsx(tmp_path / 'busy.xlsx', [['Сидоров Семён', 'Склад', None, 'busy@example.test', None]])
    rep = import_employees(session, p, DIM)
    assert rep.problems == ['строка 2: e-mail занят другим сотрудником']
    assert len(session.scalars(select(ChannelId)).all()) == 1
