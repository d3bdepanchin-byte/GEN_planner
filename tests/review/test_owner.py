from datetime import date, datetime, timezone

import openpyxl
import pytest
from sqlalchemy import select

from planerka.core.numbering import next_task_number
from planerka.core.statuses import TaskStatus
from planerka.db.models import History, ReviewFile, Task, TaskEdit
from planerka.review.executor import ReviewErrors, ReviewRejected
from planerka.review.owner import (
    ParsedOwnerReview, apply_owner_review, build_owner_file, parse_owner_file,
)

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def make_task(session, owner, title, status=TaskStatus.CANDIDATE.value, **kw):
    kw.setdefault('type', 'поручение')
    kw.setdefault('questions', [])
    kw.setdefault('flags', [])
    t = Task(number=next_task_number(session), title=title, owner_id=owner.id if owner else None,
             due_date=date(2026, 11, 14), status=status, **kw)
    session.add(t)
    session.flush()
    return t


@pytest.fixture
def owner(employees):
    return employees[1]


@pytest.fixture
def smirnov(employees):
    return employees[0]


@pytest.fixture
def confirmed_task(session, smirnov):
    return make_task(session, smirnov, 'Подготовить прайс-лист',
                     TaskStatus.CONFIRMED_BY_EXECUTOR.value, priority='B')


@pytest.fixture
def no_owner_task(session):
    return make_task(session, None, 'Заказать мерч', questions=['ответственный не назван'])


@pytest.fixture
def ambiguous_task(session):
    return make_task(session, None, 'Сверить склад',
                     questions=['выбрать ответственного: [P1|P3]'])


@pytest.fixture
def reassigned_task(session, smirnov):
    return make_task(session, smirnov, 'Передать клиента', flags=['переназначена'],
                     questions=['исполнитель предлагает передать задачу: Попов Александр'])


@pytest.fixture
def rejected_task(session, smirnov):
    return make_task(session, smirnov, 'Старая задача', flags=['отклонена исполнителем'])


@pytest.fixture
def escalated_task(session, smirnov):
    t = make_task(session, smirnov, 'Не ответили')
    session.add(ReviewFile(kind='executor', employee_id=smirnov.id, path='x',
                           task_numbers=[t.number], escalated_at=NOW))
    session.flush()
    return t


@pytest.fixture
def plain_candidate(session, smirnov):
    """Ушла исполнителю, ответа ещё ждём: владельцу не показывается."""
    t = make_task(session, smirnov, 'Ждём исполнителя')
    session.add(ReviewFile(kind='executor', employee_id=smirnov.id, path='x',
                           task_numbers=[t.number]))
    session.flush()
    return t


@pytest.fixture
def suggested_closed_task(session, smirnov):
    return make_task(session, smirnov, 'Закрытая тема', TaskStatus.CONFIRMED_BY_EXECUTOR.value,
                     registry_suggested='закрытый')


def fill(path, rows):
    """rows: {номер: {заголовок: значение}}."""
    wb = openpyxl.load_workbook(path)
    ws = wb['Задачи']
    head = {c.value: c.column for c in ws[1]}
    for r in range(2, ws.max_row + 1):
        num = int(str(ws.cell(r, head['Номер']).value).split('-')[1])
        for col, val in rows.get(num, {}).items():
            ws.cell(r, head[col]).value = val
    wb.save(path)


def returned(review, rows):
    fill(review.path, rows)
    parsed = parse_owner_file(review.path)
    assert isinstance(parsed, ParsedOwnerReview), parsed
    return review, parsed


def tasks_in(session, review):
    return list(session.scalars(select(Task).where(Task.number.in_(review.task_numbers))
                                .order_by(Task.number)))


def history_states(session, t):
    return [h.old for h in session.scalars(select(History).where(History.task_id == t.id))
            if h.event == 'status'] + [h.new for h in session.scalars(
                select(History).where(History.task_id == t.id)) if h.event == 'status']


def sections(review):
    ws = openpyxl.load_workbook(review.path)['Задачи']
    head = {c.value: c.column for c in ws[1]}
    return {ws.cell(r, head['Номер']).value: ws.cell(r, head['Раздел']).value
            for r in range(2, ws.max_row + 1)}


def test_owner_file_has_confirmed_and_disputed(session, owner, confirmed_task, no_owner_task,
                                               tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    assert {confirmed_task.number, no_owner_task.number} <= set(rf.task_numbers)
    assert rf.kind == 'owner' and rf.employee_id == owner.id
    assert rf.path.endswith(f'реестр_{rf.id}.xlsx')


def test_nothing_to_decide_returns_none(session, owner, plain_candidate, tmp_path):
    assert build_owner_file(session, owner, tmp_path, NOW) is None


def test_sections_and_single_appearance(session, owner, confirmed_task, no_owner_task,
                                        ambiguous_task, reassigned_task, rejected_task,
                                        escalated_task, plain_candidate, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    by_section = {int(k.split('-')[1]): v for k, v in sections(rf).items()}
    assert by_section == {
        confirmed_task.number: 'подтверждена исполнителем',
        no_owner_task.number: 'спорная: нет ответственного',
        ambiguous_task.number: 'спорная: неоднозначное имя',
        reassigned_task.number: 'переназначена',
        rejected_task.number: 'отклонена исполнителем',
        escalated_task.number: 'не вернулся файл',
    }
    assert len(rf.task_numbers) == 6


def test_decided_tasks_not_in_file(session, owner, smirnov, confirmed_task, tmp_path):
    make_task(session, smirnov, 'В работе', TaskStatus.IN_PROGRESS.value)
    make_task(session, smirnov, 'Отклонена', TaskStatus.REJECTED.value, flags=['переназначена'])
    rf = build_owner_file(session, owner, tmp_path, NOW)
    assert rf.task_numbers == [confirmed_task.number]


def test_returned_executor_file_not_listed_as_missing(session, owner, smirnov, tmp_path):
    t = make_task(session, smirnov, 'Вернули')
    session.add(ReviewFile(kind='executor', employee_id=smirnov.id, path='x',
                           task_numbers=[t.number], escalated_at=NOW, returned_at=NOW))
    session.flush()
    assert build_owner_file(session, owner, tmp_path, NOW) is None


def test_file_layout_and_prefill(session, owner, suggested_closed_task, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    wb = openpyxl.load_workbook(rf.path)
    ws = wb['Задачи']
    head = [c.value for c in ws[1]]
    for h in ('Номер', 'Задача', 'Ответственный', 'Срок', 'Раздел', 'Вопросы', 'Реестр',
              'Приоритет', 'Решение', 'Комментарий'):
        assert h in head
    col = {h: i + 1 for i, h in enumerate(head)}
    rows = {ws.cell(r, 1).value: r for r in range(2, ws.max_row + 1)}
    r = rows[f'И-{suggested_closed_task.number:03d}']
    assert ws.cell(r, col['Реестр']).value == 'закрытый'
    assert ws.cell(r, col['Ответственный']).value == 'Смирнов Александр'
    assert suggested_closed_task.registry == 'общий'
    assert len(ws.data_validations.dataValidation) == 4
    assert wb['Справочник'].sheet_state == 'hidden'
    assert 'Как заполнить' in wb.sheetnames


def test_approve_moves_to_in_progress_with_history(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'утверждаю'}})
    report = apply_owner_review(session, review, parsed, now=NOW)
    t = tasks_in(session, review)[0]
    assert t.status == 'в работе'
    assert 'подтверждена исполнителем' in history_states(session, t)
    assert (report.approved, report.rejected) == (1, 0)
    assert review.returned_at == NOW


def test_reject_is_final_and_number_not_reused(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'Отклоняю'}})
    report = apply_owner_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert t.status == 'отклонена' and next_task_number(session) > t.number
    assert (report.approved, report.rejected) == (0, 1)


def test_closed_registry_set_by_owner(session, owner, suggested_closed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {suggested_closed_task.number: {'Решение': 'утверждаю'}})
    apply_owner_review(session, review, parsed)
    assert tasks_in(session, review)[0].registry == 'закрытый'


def test_owner_can_override_suggestion_and_priority(session, owner, suggested_closed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {suggested_closed_task.number: {
        'Решение': 'утверждаю', 'Реестр': 'общий', 'Приоритет': 'a', 'Комментарий': 'ок'}})
    apply_owner_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert (t.registry, t.priority) == ('общий', 'A')
    assert 'комментарий владельца реестра: ок' in t.questions


def test_llm_closed_suggestion_not_applied_before_owner(session, suggested_closed_task):
    assert suggested_closed_task.registry == 'общий'
    assert suggested_closed_task.registry_suggested == 'закрытый'


def test_disputed_without_owner_requires_choice(session, owner, no_owner_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    fill(rf.path, {no_owner_task.number: {'Решение': 'утверждаю'}})
    res = parse_owner_file(rf.path)
    assert isinstance(res, ReviewErrors) and 'выберите ответственного' in res.lines[0]
    assert res.lines[0].startswith('строка 2:')


def test_disputed_reject_needs_no_owner(session, owner, no_owner_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {no_owner_task.number: {'Решение': 'отклоняю'}})
    apply_owner_review(session, review, parsed)
    assert tasks_in(session, review)[0].status == 'отклонена'


def test_disputed_with_chosen_owner_sets_owner(session, owner, ambiguous_task, smirnov, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {ambiguous_task.number: {
        'Решение': 'утверждаю', 'Ответственный': 'смирнов александр'}})
    apply_owner_review(session, review, parsed)
    t = tasks_in(session, review)[0]
    assert t.owner_id == smirnov.id and t.status == 'в работе'


def test_unknown_owner_rejected_nothing_changed(session, owner, confirmed_task, no_owner_task,
                                                tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {
        confirmed_task.number: {'Решение': 'утверждаю'},
        no_owner_task.number: {'Решение': 'утверждаю', 'Ответственный': 'Иванов Пётр'}})
    with pytest.raises(ReviewRejected) as exc:
        apply_owner_review(session, review, parsed)
    assert 'не найден' in exc.value.errors.lines[0]
    assert confirmed_task.status == 'подтверждена исполнителем' and review.returned_at is None


def test_invalid_registry_and_priority_are_errors(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    fill(rf.path, {confirmed_task.number: {'Решение': 'утверждаю', 'Реестр': 'секретный',
                                           'Приоритет': 'Z'}})
    res = parse_owner_file(rf.path)
    assert isinstance(res, ReviewErrors) and len(res.lines) == 2


def test_missing_decision_is_not_error(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    res = parse_owner_file(rf.path)
    assert isinstance(res, ParsedOwnerReview) and res.rows == []  # F5: строка остаётся как есть


def test_regulation_priority_must_be_empty(session, owner, smirnov, tmp_path):
    t = make_task(session, smirnov, 'Регламент отпусков', TaskStatus.CONFIRMED_BY_EXECUTOR.value,
                  type='регламент')
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {t.number: {'Решение': 'утверждаю', 'Приоритет': 'A'}})
    with pytest.raises(ReviewRejected) as exc:
        apply_owner_review(session, review, parsed)
    assert 'регламент' in exc.value.errors.lines[0]
    assert t.status == 'подтверждена исполнителем'


def test_apply_twice_is_idempotent(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'утверждаю'}})
    apply_owner_review(session, review, parsed)
    again = apply_owner_review(session, review, parsed)
    assert (again.approved, again.skipped) == (0, 1)


def test_foreign_file_is_not_from_system(tmp_path):
    p = tmp_path / 'a.xlsx'
    openpyxl.Workbook().save(p)
    res = parse_owner_file(p)
    assert isinstance(res, ReviewErrors) and 'не из системы' in res.lines[0]


def test_wrong_review_id_rejected(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'утверждаю'}})
    parsed.review_id += 1
    with pytest.raises(ReviewRejected):
        apply_owner_review(session, review, parsed)


# --- F5: пустое решение, разделы, шаг исполнителя пропущен ---

SKIP_NOTE = 'шаг исполнителя пропущен: решение владельца реестра'


def cell(review, number, column):
    ws = openpyxl.load_workbook(review.path)['Задачи']
    head = {c.value: c.column for c in ws[1]}
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, head['Номер']).value == f'И-{number:03d}':
            return ws.cell(r, head[column]).value
    raise KeyError(number)


def notes(session, t):
    return [h.new for h in session.scalars(select(History).where(
        History.task_id == t.id, History.event == 'примечание'))]


def test_empty_decision_row_left_as_is(session, owner, confirmed_task, smirnov, tmp_path):
    other = make_task(session, smirnov, 'Подумать', TaskStatus.CONFIRMED_BY_EXECUTOR.value)
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'утверждаю'}})
    assert [r.number for r in parsed.rows] == [confirmed_task.number]
    report = apply_owner_review(session, review, parsed, now=NOW)
    assert (report.approved, report.rejected) == (1, 0)
    assert other.status == 'подтверждена исполнителем'


def test_reassigned_and_rejected_have_empty_owner_cell(session, owner, confirmed_task,
                                                       reassigned_task, rejected_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    assert cell(rf, confirmed_task.number, 'Ответственный') == 'Смирнов Александр'
    assert cell(rf, reassigned_task.number, 'Ответственный') is None
    assert cell(rf, rejected_task.number, 'Ответственный') is None


@pytest.mark.parametrize('fixture', ['reassigned_task', 'rejected_task'])
def test_reassigned_or_rejected_approve_needs_owner(session, owner, fixture, request, tmp_path):
    t = request.getfixturevalue(fixture)
    rf = build_owner_file(session, owner, tmp_path, NOW)
    fill(rf.path, {t.number: {'Решение': 'утверждаю'}})
    res = parse_owner_file(rf.path)
    assert isinstance(res, ReviewErrors)
    assert res.lines == ['строка 2: выберите ответственного']


@pytest.mark.parametrize('fixture', ['reassigned_task', 'rejected_task'])
def test_reassigned_or_rejected_approved_with_owner(session, owner, fixture, request, employees,
                                                    tmp_path):
    t = request.getfixturevalue(fixture)
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {t.number: {'Решение': 'утверждаю',
                                              'Ответственный': 'Попов Александр'}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert (t.status, t.owner_id) == ('в работе', employees[2].id)
    assert notes(session, t) == [SKIP_NOTE]


def test_not_returned_cannot_be_approved(session, owner, escalated_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    fill(rf.path, {escalated_task.number: {'Решение': 'утверждаю'}})
    res = parse_owner_file(rf.path)
    assert isinstance(res, ReviewErrors)
    assert res.lines == ['строка 2: задача ждёт исполнителя — вынесите на планёрку или отклоните']


@pytest.mark.parametrize('decision, status', [(None, 'на подтверждении'),
                                              ('отклоняю', 'отклонена')])
def test_not_returned_empty_or_reject(session, owner, escalated_task, decision, status, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {escalated_task.number: {'Решение': decision}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert escalated_task.status == status


def test_edited_section_cell_does_not_bypass_rules(session, owner, escalated_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {escalated_task.number: {
        'Решение': 'утверждаю', 'Раздел': 'подтверждена исполнителем'}})
    with pytest.raises(ReviewRejected) as exc:
        apply_owner_review(session, review, parsed, now=NOW)
    assert exc.value.errors.lines == [
        'строка 2: задача ждёт исполнителя — вынесите на планёрку или отклоните']
    assert escalated_task.status == 'на подтверждении'


def test_disputed_approval_records_skipped_executor_step(session, owner, no_owner_task, smirnov,
                                                         tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {no_owner_task.number: {
        'Решение': 'утверждаю', 'Ответственный': 'Смирнов Александр'}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert notes(session, no_owner_task) == [SKIP_NOTE]


def test_confirmed_approval_has_no_skip_note(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'утверждаю'}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert notes(session, confirmed_task) == []


def test_howto_explains_sections(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    text = '\n'.join(r[0].value for r in openpyxl.load_workbook(rf.path)['Как заполнить'].iter_rows())
    assert 'Пустое «Решение»' in text
    assert '«переназначена», «отклонена исполнителем»' in text
    assert 'вынесите на планёрку или отклоните' in text
    assert '«Срок»' in text and '«Правки к задачам»' in text


# --- F6: «Задача» и «Срок» владельца применяются ---

def edits_of(session, t):
    return [(h.old, h.new, h.actor) for h in session.scalars(select(History).where(
        History.task_id == t.id, History.event == 'правка').order_by(History.id))]


@pytest.mark.parametrize('due', ['20.11.2026', datetime(2026, 11, 20)])
def test_owner_title_and_due_applied(session, owner, confirmed_task, due, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {
        'Решение': 'утверждаю', 'Задача': 'Подготовить прайс-лист на 2027', 'Срок': due}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert confirmed_task.title == 'Подготовить прайс-лист на 2027'
    assert confirmed_task.due_date == date(2026, 11, 20)
    actor = f'владелец реестра:{owner.id}'
    assert edits_of(session, confirmed_task) == [
        ('название: Подготовить прайс-лист', 'название: Подготовить прайс-лист на 2027', actor),
        ('срок: 2026-11-14', 'срок: 2026-11-20', actor)]


def test_unchanged_title_and_due_leave_no_history(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'утверждаю'}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert edits_of(session, confirmed_task) == []


def test_owner_bad_due_is_error(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    fill(rf.path, {confirmed_task.number: {'Решение': 'утверждаю', 'Срок': 'завтра'}})
    res = parse_owner_file(rf.path)
    assert isinstance(res, ReviewErrors)
    assert res.lines == ['строка 2: срок «завтра» не распознан, нужен формат дд.мм.гггг']


def test_rejected_row_edits_not_applied(session, owner, confirmed_task, tmp_path):
    rf = build_owner_file(session, owner, tmp_path, NOW)
    review, parsed = returned(rf, {confirmed_task.number: {'Решение': 'отклоняю',
                                                           'Задача': 'Другое'}})
    apply_owner_review(session, review, parsed, now=NOW)
    assert confirmed_task.title == 'Подготовить прайс-лист'


# --- F7: правки к задачам — лист для сведения ---

def test_task_edits_sheet_for_information(session, owner, confirmed_task, smirnov, tmp_path):
    target = make_task(session, smirnov, 'В работе', TaskStatus.IN_PROGRESS.value)
    session.add_all([
        TaskEdit(task_number=target.number, change='срок перенести на 01.12', quote='давай к первому',
                 timecode='[02:10]', status='на подтверждении'),
        TaskEdit(task_number=target.number, change='уже передана', status='передана владельцу')])
    session.flush()
    rf = build_owner_file(session, owner, tmp_path, NOW)
    ws = openpyxl.load_workbook(rf.path)['Правки к задачам']
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    assert rows == [['Номер', 'Что меняется', 'Цитата', 'Таймкод'],
                    [f'И-{target.number:03d}', 'срок перенести на 01.12', 'давай к первому',
                     '[02:10]']]
    assert target.number not in rf.task_numbers  # правки не решаются в этом файле


def test_file_with_only_task_edits(session, owner, smirnov, tmp_path):
    target = make_task(session, smirnov, 'В работе', TaskStatus.IN_PROGRESS.value)
    session.add(TaskEdit(task_number=target.number, change='добавить склад 2',
                         status='на подтверждении'))
    session.flush()
    rf = build_owner_file(session, owner, tmp_path, NOW)
    assert rf is not None and rf.task_numbers == []
    assert 'Правки к задачам' in openpyxl.load_workbook(rf.path).sheetnames
