from datetime import date, datetime, timezone

import openpyxl
import pytest
from sqlalchemy import select

from planerka.core.numbering import next_task_number
from planerka.core.statuses import TaskStatus
from planerka.db.models import History, ReviewFile, Task
from planerka.review.executor import (
    ParsedReview, ReviewDecision, ReviewErrors, ReviewRejected,
    apply_executor_review, build_executor_file, normalize_decision,
    parse_executor_file, read_meta,
)

COLS = {'номер': 1, 'решение': 10, 'задача': 11, 'срок': 12, 'ответственный': 13, 'комментарий': 14}


def make_task(session, owner, title):
    t = Task(number=next_task_number(session), title=title, type='поручение', priority='B',
             owner_id=owner.id if owner else None, due_date=date(2026, 11, 14),
             done_criteria='готово', quote='Цитата с планёрки', questions=['срок уточнить'],
             status=TaskStatus.CANDIDATE.value)
    session.add(t)
    session.flush()
    return t


@pytest.fixture
def smirnov(employees):
    return employees[0]


@pytest.fixture
def smirnov_tasks(session, smirnov):
    return [make_task(session, smirnov, 'Подготовить прайс-лист'),
            make_task(session, smirnov, 'Согласовать график поставок')]


@pytest.fixture
def built(session, smirnov, smirnov_tasks, tmp_path):
    return build_executor_file(session, smirnov, smirnov_tasks, tmp_path)


def fill(path, rows):
    """rows: {номер задачи: {колонка: значение}} — как заполнил бы человек."""
    wb = openpyxl.load_workbook(path)
    ws = wb['Задачи']
    for r in range(2, ws.max_row + 1):
        num = ws.cell(r, 1).value
        for col, val in rows.get(int(str(num).split('-')[1]) if num else None, {}).items():
            ws.cell(r, COLS[col]).value = val
    wb.save(path)


def returned(session, review, rows):
    fill(review.path, rows)
    parsed = parse_executor_file(review.path)
    assert isinstance(parsed, ParsedReview), parsed
    return review, parsed


def tasks_in(session, review):
    return list(session.scalars(select(Task).where(Task.number.in_(review.task_numbers))
                                .order_by(Task.number)))


def nums(tasks):
    return [t.number for t in tasks]


@pytest.fixture
def returned_confirm(session, built, smirnov_tasks):
    return returned(session, built, {t.number: {'решение': 'подтверждаю'} for t in smirnov_tasks})


@pytest.fixture
def returned_edit_due(session, built, smirnov_tasks):
    rows = {smirnov_tasks[0].number: {'решение': 'правка', 'срок': date(2026, 11, 20)},
            smirnov_tasks[1].number: {'решение': 'подтверждаю'}}
    return returned(session, built, rows)


@pytest.fixture
def returned_reassign(session, built, smirnov_tasks, employees):
    rows = {smirnov_tasks[0].number: {'решение': 'правка', 'ответственный': employees[1].full_name},
            smirnov_tasks[1].number: {'решение': 'подтверждаю'}}
    return returned(session, built, rows)


@pytest.fixture
def returned_reject(session, built, smirnov_tasks):
    rows = {smirnov_tasks[0].number: {'решение': 'отклоняю', 'комментарий': 'не моя зона'},
            smirnov_tasks[1].number: {'решение': 'подтверждаю'}}
    return returned(session, built, rows)


def test_file_has_only_this_executor_tasks(session, smirnov, smirnov_tasks, tmp_path):
    rf = build_executor_file(session, smirnov, smirnov_tasks, tmp_path)
    assert set(rf.task_numbers) == {t.number for t in smirnov_tasks}
    assert rf.kind == 'executor' and rf.employee_id == smirnov.id
    assert rf.sent_at is None and rf.due_at is None


def test_file_name_by_ids_without_names(built, smirnov, tmp_path):
    assert built.path.endswith(f'уточнение_{smirnov.id}_{built.id}.xlsx')
    assert 'Смирнов' not in built.path


def test_foreign_task_not_allowed_in_file(session, smirnov, employees, tmp_path):
    foreign = make_task(session, employees[1], 'Чужая задача')
    with pytest.raises(ValueError):
        build_executor_file(session, smirnov, [foreign], tmp_path)


def test_decision_column_has_dropdown(session, smirnov, smirnov_tasks, tmp_path):
    rf = build_executor_file(session, smirnov, smirnov_tasks, tmp_path)
    ws = openpyxl.load_workbook(rf.path)['Задачи']
    assert any('подтверждаю' in dv.formula1 for dv in ws.data_validations.dataValidation)


def test_owner_dropdown_lists_all_employees_on_hidden_sheet(built, employees):
    wb = openpyxl.load_workbook(built.path)
    ws = wb['Задачи']
    owner_dv = [dv for dv in ws.data_validations.dataValidation if 'M' in str(dv.sqref)]
    assert owner_dv and owner_dv[0].formula1.startswith('=')
    hidden = [s for s in wb.worksheets if s.sheet_state == 'hidden' and s.title != 'meta']
    assert hidden
    listed = {c.value for row in hidden[0].iter_rows() for c in row if c.value}
    assert listed == {e.full_name for e in employees}


def test_layout_header_frozen_and_instruction_sheet(built):
    wb = openpyxl.load_workbook(built.path)
    ws = wb['Задачи']
    assert ws.freeze_panes == 'A2' and ws['A1'].font.bold
    assert [c.value for c in ws[1]][:2] == ['Номер', 'Задача']
    assert ws['I1'].value == 'Вопросы' and ws['H2'].alignment.wrap_text
    assert 'Как заполнить' in wb.sheetnames


def test_meta_sheet_hidden_and_readable(built):
    wb = openpyxl.load_workbook(built.path)
    assert wb['meta'].sheet_state == 'hidden'
    rid, numbers = read_meta(built.path)
    assert rid == built.id and numbers == sorted(built.task_numbers)


def test_file_without_meta_is_error(tmp_path):
    p = tmp_path / 'x.xlsx'
    wb = openpyxl.Workbook()
    wb.active.title = 'Задачи'
    wb.save(p)
    res = parse_executor_file(p)
    assert isinstance(res, ReviewErrors) and 'не из системы' in res.lines[0]


def test_broken_file_is_error_not_exception(tmp_path):
    p = tmp_path / 'x.xlsx'
    p.write_bytes(b'not a zip')
    res = parse_executor_file(p)
    assert isinstance(res, ReviewErrors) and 'не из системы' in res.lines[0]


def test_normalize_decision():
    assert normalize_decision('  Подтверждаю ') is ReviewDecision.CONFIRM
    assert normalize_decision('ПРАВКА') is ReviewDecision.EDIT
    assert normalize_decision('отклоняю') is ReviewDecision.REJECT
    assert normalize_decision('Подтверждаю  ') is ReviewDecision.CONFIRM
    assert normalize_decision('да') is None and normalize_decision('') is None


def test_confirm_sets_confirmed_by_executor(session, returned_confirm):
    review, parsed = returned_confirm
    rep = apply_executor_review(session, review, parsed)
    assert all(t.status == 'подтверждена исполнителем' for t in tasks_in(session, review))
    assert rep.confirmed == 2 and rep.to_owner == 0


def test_confirm_sets_returned_at(session, returned_confirm):
    review, parsed = returned_confirm
    now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    apply_executor_review(session, review, parsed, now=now)
    assert review.returned_at == now


def test_edit_applies_fields_and_confirms(session, returned_edit_due):
    review, parsed = returned_edit_due
    apply_executor_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert t.due_date == date(2026, 11, 20) and t.status == 'подтверждена исполнителем'
    h = session.scalars(select(History).where(History.task_id == t.id, History.event == 'правка')).all()
    assert len(h) == 1 and '2026-11-14' in h[0].old and '2026-11-20' in h[0].new


def test_edit_title_writes_history(session, built, smirnov_tasks):
    review, parsed = returned(session, built, {
        smirnov_tasks[0].number: {'решение': 'правка', 'задача': 'Новое название'},
        smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    apply_executor_review(session, review, parsed)
    assert tasks_in(session, review)[0].title == 'Новое название'


def test_reassign_goes_to_owner(session, returned_reassign, smirnov):
    review, parsed = returned_reassign
    rep = apply_executor_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert rep.to_owner == 1 and rep.confirmed == 1
    assert 'переназначена' in t.flags
    assert t.owner_id == smirnov.id and t.status == 'на подтверждении'
    assert any('Кузнецова' in q for q in t.questions)


def test_reassign_to_self_is_not_reassign(session, built, smirnov_tasks, smirnov):
    review, parsed = returned(session, built, {
        smirnov_tasks[0].number: {'решение': 'правка', 'ответственный': smirnov.full_name.upper()},
        smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    rep = apply_executor_review(session, review, parsed)
    assert rep.to_owner == 0


def test_reject_goes_to_owner_not_final(session, returned_reject):
    review, parsed = returned_reject
    rep = apply_executor_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert t.status == 'на подтверждении' and 'отклонена исполнителем' in t.flags
    assert rep.to_owner == 1
    assert 'комментарий исполнителя: не моя зона' in t.questions


def test_unknown_owner_name_rejects_whole_file(session, built, smirnov_tasks):
    review, parsed = returned(session, built, {
        smirnov_tasks[0].number: {'решение': 'правка', 'ответственный': 'Иванов Пётр'},
        smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    with pytest.raises(ReviewRejected) as e:
        apply_executor_review(session, review, parsed)
    assert e.value.errors.lines == ['строка 2: ответственный не найден в справочнике']
    assert all(t.status == 'на подтверждении' for t in tasks_in(session, review))
    assert review.returned_at is None


def test_owner_name_matches_case_and_yo_insensitive(session, built, smirnov_tasks, employees):
    employees[1].full_name = 'Кузнецова Ольга Фёдоровна'
    session.flush()
    review, parsed = returned(session, built, {
        smirnov_tasks[0].number: {'решение': 'правка', 'ответственный': 'кузнецова ольга федоровна'},
        smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    assert apply_executor_review(session, review, parsed).to_owner == 1


def test_second_return_is_idempotent(session, returned_confirm):
    review, parsed = returned_confirm
    apply_executor_review(session, review, parsed)
    rep = apply_executor_review(session, review, parsed)
    assert rep.skipped == 2 and rep.confirmed == 0
    h = session.scalars(select(History).where(History.event == 'status')).all()
    assert len(h) == 2


def test_foreign_row_number_rejected(session, built, smirnov_tasks, employees):
    foreign = make_task(session, employees[1], 'Чужая задача')
    fill(built.path, {t.number: {'решение': 'подтверждаю'} for t in smirnov_tasks})
    wb = openpyxl.load_workbook(built.path)
    wb['Задачи'].append([f'И-{foreign.number:03d}'] + [None] * 8 + ['подтверждаю'])
    wb.save(built.path)
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors)
    assert res.lines == [f'строка 4: задачи И-{foreign.number:03d} нет в этом файле']


def test_empty_decision_is_error(session, built, smirnov_tasks):
    fill(built.path, {smirnov_tasks[0].number: {'решение': 'подтверждаю'}})
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors) and res.lines == ['строка 3: не выбрано решение']


def test_unknown_decision_is_error(session, built, smirnov_tasks):
    fill(built.path, {smirnov_tasks[0].number: {'решение': 'ок'},
                      smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors) and res.lines[0].startswith('строка 2: решение «ок»')


def test_edit_without_fields_is_error(session, built, smirnov_tasks):
    fill(built.path, {smirnov_tasks[0].number: {'решение': 'правка', 'комментарий': 'просто так'},
                      smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors) and res.lines == ['строка 2: выбрана правка, но поле «Правка: …» не заполнено']


def test_confirm_with_edit_fields_is_error(session, built, smirnov_tasks):
    fill(built.path, {smirnov_tasks[0].number: {'решение': 'подтверждаю', 'срок': '20.11.2026'},
                      smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors) and res.lines[0].startswith('строка 2:')


def test_bad_date_is_error(session, built, smirnov_tasks):
    fill(built.path, {smirnov_tasks[0].number: {'решение': 'правка', 'срок': 'на той неделе'},
                      smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors) and res.lines[0].startswith('строка 2: срок')


def test_duplicate_number_is_error(session, built, smirnov_tasks):
    wb = openpyxl.load_workbook(built.path)
    ws = wb['Задачи']
    ws.append([f'И-{smirnov_tasks[0].number:03d}'] + [None] * 8 + ['подтверждаю'])
    wb.save(built.path)
    fill(built.path, {t.number: {'решение': 'подтверждаю'} for t in smirnov_tasks})
    res = parse_executor_file(built.path)
    assert isinstance(res, ReviewErrors) and any('повтор' in line for line in res.lines)


def test_parse_tolerates_case_spaces_and_text_dates(session, built, smirnov_tasks):
    wb = openpyxl.load_workbook(built.path)
    wb.create_sheet('Заметки')['A1'] = 'лишний лист'
    wb.save(built.path)
    fill(built.path, {smirnov_tasks[0].number: {'решение': '  Подтверждаю '},
                      smirnov_tasks[1].number: {'решение': 'ПРАВКА', 'срок': '5.11.2026'}})
    res = parse_executor_file(built.path)
    assert isinstance(res, ParsedReview)
    assert {r.decision for r in res.rows} == {ReviewDecision.CONFIRM, ReviewDecision.EDIT}
    assert [r.due_date for r in res.rows if r.due_date] == [date(2026, 11, 5)]
    assert res.review_id == built.id


def test_rows_with_empty_number_ignored(session, built, smirnov_tasks):
    fill(built.path, {t.number: {'решение': 'подтверждаю'} for t in smirnov_tasks})
    wb = openpyxl.load_workbook(built.path)
    wb['Задачи'].cell(10, 14).value = 'заметка без номера'
    wb.save(built.path)
    assert isinstance(parse_executor_file(built.path), ParsedReview)


def test_numbers_as_plain_int_accepted(session, built, smirnov_tasks):
    fill(built.path, {t.number: {'решение': 'подтверждаю'} for t in smirnov_tasks})
    wb = openpyxl.load_workbook(built.path)
    wb['Задачи']['A2'] = smirnov_tasks[0].number
    wb.save(built.path)
    assert isinstance(parse_executor_file(built.path), ParsedReview)


def test_reassign_keeps_terms_and_records_proposals(session, built, smirnov_tasks, employees):
    review, parsed = returned(session, built, {
        smirnov_tasks[0].number: {'решение': 'правка', 'ответственный': employees[1].full_name,
                                  'срок': date(2026, 11, 20), 'задача': 'Новое название'},
        smirnov_tasks[1].number: {'решение': 'подтверждаю'}})
    apply_executor_review(session, review, parsed)
    apply_executor_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert t.due_date == date(2026, 11, 14) and t.title == 'Подготовить прайс-лист'
    assert t.questions.count('исполнитель предлагает срок: 20.11.2026') == 1
    assert t.questions.count('исполнитель предлагает название: Новое название') == 1
    assert not session.scalars(select(History).where(History.task_id == t.id,
                                                     History.event == 'правка')).all()
