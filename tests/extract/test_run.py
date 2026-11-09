import json
from datetime import date

import pytest
from sqlalchemy import select

from planerka.core.numbering import next_task_number
from planerka.core.statuses import TaskStatus
from planerka.db.models import Task, TaskEdit
from planerka.extract import run as run_module
from planerka.extract.run import extract_source
from planerka.masking.checker import MaskingUncertain
from planerka.settings import set_setting


def candidate(**over) -> dict:
    """Поручение из итогового блока со всеми обязательными полями."""
    c = {
        'title': 'Подготовить прайс-лист для строительных компаний',
        'type': 'поручение',
        'priority': 'B',
        'goal_2027': 'стратегия продаж',
        'owner': '[P1]',
        'coexecutors': [],
        'author': None,
        'due_date': '2026-11-20',
        'control_date': None,
        'done_criteria': 'прайс-лист отправлен в отдел продаж',
        'timecode': '[01:00]',
        'quote': 'Ставим задачу: подготовить прайс-лист, ответственный — [P1]',
        'confidence': 'высокая',
        'confidence_reason': 'задача повторена в итогах',
        'questions': [],
        'from_final_block': True,
        'closed_registry_suggested': False,
    }
    c.update(over)
    return c


def result(candidates=None, edits=None, final_block_found=True,
           summary='Обсудили прайс-лист, готовит [P1].') -> str:
    return json.dumps({
        'final_block_found': final_block_found,
        'summary': summary,
        'candidates': [candidate()] if candidates is None else candidates,
        'edits': edits or [],
    }, ensure_ascii=False)


@pytest.fixture
def deps(make_deps):
    return make_deps(result())


@pytest.fixture
def deps_due_nov_1(make_deps):
    return make_deps(result([candidate(due_date='2026-11-01')]))


@pytest.fixture
def deps_bad_goal(make_deps):
    return make_deps(result([candidate(goal_2027='захват мира')]))


@pytest.fixture
def deps_no_due(make_deps):
    return make_deps(result([candidate(due_date=None)]))


@pytest.fixture
def deps_no_final(make_deps):
    return make_deps(result([candidate(from_final_block=False),
                             candidate(title='Сверить остатки', from_final_block=False)],
                            final_block_found=False))


@pytest.fixture
def deps_edit_21(make_deps):
    return make_deps(result(edits=[{'task_number': 'И-021', 'change': 'срок — 2026-11-25',
                                    'quote': 'Задача И-021 — срок двадцать пятое',
                                    'timecode': '[01:30]'}]))


def tasks_of(session, source):
    return session.scalars(select(Task).where(Task.source_id == source.id)
                           .order_by(Task.number)).all()


def first_task(session, source):
    return tasks_of(session, source)[0]


# --- тесты из брифа ---

def test_candidates_created_with_numbers_and_status(session, deps, source, transcript):
    rep = extract_source(session, source, transcript, deps)
    tasks = session.scalars(select(Task).where(Task.source_id == source.id)).all()
    assert sorted(t.number for t in tasks) == sorted(rep.created)
    assert all(t.status == 'на подтверждении' for t in tasks)


def test_owner_token_demasked_to_employee(session, deps, source, transcript, smirnov):
    extract_source(session, source, transcript, deps)
    assert first_task(session, source).owner_id == smirnov.id


def test_due_before_meeting_date_flagged(session, deps_due_nov_1, source, transcript):
    extract_source(session, source, transcript, deps_due_nov_1)
    t = first_task(session, source)
    assert t.due_date is None and any('срок' in q for q in t.questions)


def test_goal_outside_list_emptied_with_question(session, deps_bad_goal, source, transcript):
    extract_source(session, source, transcript, deps_bad_goal)
    t = first_task(session, source)
    assert t.goal_2027 is None and t.questions


def test_poruchenie_without_due_gets_question(session, deps_no_due, source, transcript):
    extract_source(session, source, transcript, deps_no_due)
    assert first_task(session, source).questions


def test_no_final_block_flags_all_candidates(session, deps_no_final, source, transcript):
    extract_source(session, source, transcript, deps_no_final)
    assert all('итоги не зафиксированы' in t.flags for t in tasks_of(session, source))


def test_edit_to_existing_task_stored_not_applied(session, deps_edit_21, source, transcript,
                                                  task_21):
    extract_source(session, source, transcript, deps_edit_21)
    edit = session.scalars(select(TaskEdit)).one()
    assert (edit.task_number, edit.status) == (21, 'на подтверждении')
    assert session.get(Task, task_21.id).due_date == task_21.due_date


def test_llm_request_has_no_directory_names(session, deps, source, transcript_with_names):
    extract_source(session, source, transcript_with_names, deps)
    sent = deps.llm.requests[0]
    assert not any(v in sent for e in deps.directory for v in e.variants)


def test_prompt_version_recorded(session, deps, source, transcript):
    extract_source(session, source, transcript, deps)
    assert source.flags['prompt'] == 'extract_tasks.v1'


# --- запрос в LLM ---

def test_request_carries_meeting_date_goals_and_masked_transcript(session, deps, source,
                                                                  transcript_with_names):
    extract_source(session, source, transcript_with_names, deps)
    sent = deps.llm.requests[0]
    assert '2026-11-11' in sent
    assert 'автоматизация и ИИ' in sent
    assert '[00:00] Начинаем планёрку, ведёт [P1]' in sent


def test_masking_uncertain_stops_before_llm(session, deps, source, transcript, monkeypatch):
    def unsure(*_):
        raise MaskingUncertain(1)
    monkeypatch.setattr(run_module, 'ensure_clean', unsure)
    with pytest.raises(MaskingUncertain):
        extract_source(session, source, transcript, deps)
    assert deps.llm.requests == []
    assert tasks_of(session, source) == [] and source.status == 'распознано'


def test_already_extracted_source_not_extracted_again(session, deps, source, transcript):
    source.status = 'разобран'
    rep = extract_source(session, source, transcript, deps)
    assert (rep.created, rep.edits, rep.notes) == ([], 0, [])
    assert deps.llm.requests == [] and tasks_of(session, source) == []


def test_source_marked_extracted_with_demasked_summary(session, deps, source, transcript):
    extract_source(session, source, transcript, deps)
    assert source.status == 'разобран'
    assert source.summary == 'Обсудили прайс-лист, готовит Смирнов Александр.'
    assert 'итоги не зафиксированы' not in source.flags


# --- люди: токен → сотрудник ---

def test_coexecutors_and_author_resolved_from_tokens(session, make_deps, source, transcript,
                                                     employees):
    deps = make_deps(result([candidate(coexecutors=['[P2]', '[P3]', '[P2]'], author='[P3]')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.coexecutor_ids == [employees[1].id, employees[2].id]
    assert t.author_id == employees[2].id


def test_ambiguous_owner_left_for_choice(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(owner='[P1|P3]')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.owner_id is None
    assert 'выбрать ответственного: Смирнов Александр или Попов Александр' in t.questions


def test_unknown_owner_token_emptied_with_question(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(owner='[P99]', coexecutors=['[P42]'])]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.owner_id is None and t.coexecutor_ids == []
    assert 'ответственный: токена нет в записи' in t.questions
    assert 'соисполнитель: токена нет в записи' in t.questions


def test_external_person_as_owner_emptied_with_question(session, make_deps, tokens, source,
                                                        transcript):
    x = tokens.external(source.id, 'Петренко Игорь')
    deps = make_deps(result([candidate(owner=x)]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.owner_id is None
    assert 'ответственный не из справочника: Петренко Игорь' in t.questions


# --- текстовые поля: demask ---

def test_text_fields_demasked(session, make_deps, tokens, source, transcript):
    x = tokens.external(source.id, 'Петренко Игорь')
    deps = make_deps(result([candidate(title=f'Позвонить {x}',
                                       questions=['уточнить у [P2] объём'])]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.title == 'Позвонить Петренко Игорь'
    assert t.quote == 'Ставим задачу: подготовить прайс-лист, ответственный — Смирнов Александр'
    assert t.questions == ['уточнить у Кузнецова Ольга объём']


def test_demask_notes_go_to_candidate_questions(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(quote='[P1|P3] берёт', done_criteria='звонок [X9]')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.quote == 'Смирнов Александр или Попов Александр берёт'
    assert t.done_criteria == 'звонок [?]'
    assert 'выбрать: Смирнов Александр или Попов Александр' in t.questions
    assert 'токена нет в записи' in t.questions


# --- постобработка по правилу 2 ---

def test_control_date_before_meeting_flagged(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(type='проект', control_date='2026-11-10')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.control_date is None and any('контрольная дата' in q for q in t.questions)


def test_relative_due_on_meeting_day_kept(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(due_date='2026-11-11')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.due_date == date(2026, 11, 11) and t.questions == []


def test_poruchenie_without_done_criteria_gets_question(session, make_deps, source,
                                                        transcript):
    deps = make_deps(result([candidate(done_criteria=None)]))
    extract_source(session, source, transcript, deps)
    assert any('критери' in q for q in first_task(session, source).questions)


@pytest.mark.parametrize('kind', ['проект', 'разбирательство'])
def test_project_and_investigation_need_control_date(session, make_deps, source, transcript,
                                                     kind):
    deps = make_deps(result([candidate(type=kind, due_date=None, done_criteria=None)]))
    extract_source(session, source, transcript, deps)
    assert first_task(session, source).questions == ['нет контрольной даты']


def test_reglament_priority_emptied_with_question(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(type='регламент', priority='A', due_date=None)]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.priority is None and any('приоритет' in q for q in t.questions)


def test_goal_matched_without_case_and_yo_stored_canonical(session, make_deps, source,
                                                           transcript):
    set_setting(session, 'goals.goals_2027', ['стратегия продаж', 'Клиентская база'], 'тест')
    deps = make_deps(result([candidate(goal_2027=' клиентская база ')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.goal_2027 == 'Клиентская база' and t.questions == []


def test_goal_with_yo_matches(session, make_deps, source, transcript):
    set_setting(session, 'goals.goals_2027', ['учёт'], 'тест')
    deps = make_deps(result([candidate(goal_2027='Учет')]))
    extract_source(session, source, transcript, deps)
    assert first_task(session, source).goal_2027 == 'учёт'


def test_no_final_block_flags_source(session, deps_no_final, source, transcript):
    extract_source(session, source, transcript, deps_no_final)
    assert source.flags['итоги не зафиксированы'] is True
    assert all('кандидат из обсуждения' in t.flags for t in tasks_of(session, source))


def test_discussion_candidate_flagged_final_one_not(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(), candidate(title='Сверить остатки',
                                                    from_final_block=False)]))
    extract_source(session, source, transcript, deps)
    final, discussed = tasks_of(session, source)
    assert final.flags == [] and discussed.flags == ['кандидат из обсуждения']


def test_closed_registry_only_suggested(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(closed_registry_suggested=True)]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert (t.registry, t.registry_suggested) == ('общий', 'закрытый')


def test_source_closed_copied_to_tasks(session, deps, source, transcript):
    source.closed = True
    extract_source(session, source, transcript, deps)
    assert first_task(session, source).source_closed is True


def test_candidate_fields_saved(session, deps, source, transcript):
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert (t.type, t.priority, t.goal_2027) == ('поручение', 'B', 'стратегия продаж')
    assert (t.due_date, t.timecode, t.confidence) == (date(2026, 11, 20), '[01:00]', 'высокая')
    assert t.questions == [] and t.flags == []


# --- больше 5 задач A на человека (Д-79) ---

def _a_tasks(session, owner, n, status=TaskStatus.IN_PROGRESS):
    for i in range(n):
        session.add(Task(number=next_task_number(session), title=f'Задача A {i}', priority='A',
                         owner_id=owner.id, status=status.value))
    session.flush()


def test_more_than_five_a_tasks_question_only(session, make_deps, source, transcript, smirnov):
    _a_tasks(session, smirnov, 5)
    deps = make_deps(result([candidate(priority='A')]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.priority == 'A' and t.owner_id == smirnov.id
    assert any('больше 5 задач A' in q for q in t.questions)


def test_five_a_tasks_with_new_ones_no_question(session, make_deps, source, transcript, smirnov):
    _a_tasks(session, smirnov, 4)
    _a_tasks(session, smirnov, 3, TaskStatus.DONE)
    deps = make_deps(result([candidate(priority='A')]))
    extract_source(session, source, transcript, deps)
    assert first_task(session, source).questions == []


def test_new_a_tasks_counted_together(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(priority='A', title=f'Задача {i}') for i in range(6)]))
    extract_source(session, source, transcript, deps)
    assert all(any('больше 5 задач A' in q for q in t.questions)
               for t in tasks_of(session, source))


# --- правки ---

def test_edit_to_unknown_number_is_report_note(session, make_deps, source, transcript):
    deps = make_deps(result(edits=[{'task_number': 'И-999', 'change': 'выполнено',
                                    'quote': 'Задача И-999 — выполнено', 'timecode': None}]))
    rep = extract_source(session, source, transcript, deps)
    assert session.scalars(select(TaskEdit)).all() == []
    assert rep.edits == 0 and any('И-999' in n for n in rep.notes)


def test_edit_text_demasked_and_linked_to_source(session, make_deps, source, transcript,
                                                 task_21):
    deps = make_deps(result(edits=[{'task_number': 'и 21', 'change': 'передать [P2]',
                                    'quote': 'И-021 — передаём [P2]', 'timecode': '[01:30]'}]))
    rep = extract_source(session, source, transcript, deps)
    edit = session.scalars(select(TaskEdit)).one()
    assert edit.change == 'передать Кузнецова Ольга'
    assert edit.quote == 'И-021 — передаём Кузнецова Ольга'
    assert (edit.source_id, edit.timecode, rep.edits) == (source.id, '[01:30]', 1)


def test_report_counts(session, deps_edit_21, source, transcript, task_21):
    rep = extract_source(session, source, transcript, deps_edit_21)
    assert len(rep.created) == 1 and rep.edits == 1 and rep.notes == []


def test_llm_answer_not_by_schema_is_retried(session, make_deps, source, transcript):
    deps = make_deps(json.dumps({'final_block_found': True}), result())
    rep = extract_source(session, source, transcript, deps)
    assert len(deps.llm.requests) == 2 and len(rep.created) == 1



# --- токены и таймкоды только из записи (фикс-раунд 1, R19, R20) ---

def test_owner_token_not_in_recording_rejected(session, make_deps, source, transcript_p1_only):
    deps = make_deps(result([candidate(owner='[P2]')]))
    extract_source(session, source, transcript_p1_only, deps)
    t = first_task(session, source)
    assert t.owner_id is None and 'ответственный: токена нет в записи' in t.questions


def test_coexecutor_and_author_not_in_recording_rejected(session, make_deps, source,
                                                        transcript_p1_only, smirnov):
    deps = make_deps(result([candidate(coexecutors=['[P1]', '[P3]'], author='[P2]')]))
    extract_source(session, source, transcript_p1_only, deps)
    t = first_task(session, source)
    assert t.coexecutor_ids == [smirnov.id] and t.author_id is None
    assert 'соисполнитель: токена нет в записи' in t.questions
    assert 'постановщик: токена нет в записи' in t.questions


def test_owner_picked_from_ambiguous_mention_rejected(session, make_deps, source,
                                                      transcript_ambiguous_only):
    deps = make_deps(result([candidate(owner='[P1]', quote='Саша, подготовь')]))
    extract_source(session, source, transcript_ambiguous_only, deps)
    t = first_task(session, source)
    assert t.owner_id is None and 'ответственный: токена нет в записи' in t.questions


def test_free_text_token_not_in_recording_becomes_unintelligible(session, make_deps, source,
                                                                 transcript_p1_only):
    deps = make_deps(result([candidate(title='Позвонить [P2]')]))
    extract_source(session, source, transcript_p1_only, deps)
    t = first_task(session, source)
    assert t.title == 'Позвонить [?]' and 'токена нет в записи' in t.questions


def test_ambiguous_token_in_summary_keeps_text(session, make_deps, source, transcript):
    deps = make_deps(result(summary='Прайс готовит [P1|P3].'))
    rep = extract_source(session, source, transcript, deps)
    assert source.summary == 'Прайс готовит Смирнов Александр или Попов Александр.'
    assert rep.notes == ['саммари: неоднозначный токен']


def test_missing_owner_and_type_asked(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(owner=None, type=None)]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert 'ответственный не назван' in t.questions and 'тип не указан' in t.questions


def test_server_questions_not_duplicated_with_llm(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(owner=None, type=None,
                                       questions=['ответственный не назван', 'тип не указан'])]))
    extract_source(session, source, transcript, deps)
    assert first_task(session, source).questions == ['ответственный не назван', 'тип не указан']


@pytest.mark.parametrize('timecode', ['[09:59]', '1:00', '[1:00]', '01:00'])
def test_timecode_not_in_recording_emptied(session, make_deps, source, transcript, timecode):
    deps = make_deps(result([candidate(timecode=timecode)]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.timecode is None and 'таймкод не найден в записи' in t.questions


def test_edit_timecode_not_in_recording_emptied(session, make_deps, source, transcript, task_21):
    deps = make_deps(result(edits=[{'task_number': 'И-021', 'change': 'выполнено',
                                    'quote': 'И-021 — выполнено', 'timecode': '[44:44]'}]))
    rep = extract_source(session, source, transcript, deps)
    assert session.scalars(select(TaskEdit)).one().timecode is None
    assert 'правка к И-021: таймкод не найден в записи' in rep.notes


def test_null_timecode_asked(session, make_deps, source, transcript):
    deps = make_deps(result([candidate(timecode=None)]))
    extract_source(session, source, transcript, deps)
    t = first_task(session, source)
    assert t.timecode is None and 'таймкод не указан' in t.questions
    assert 'таймкод не найден в записи' not in t.questions


def test_edit_null_timecode_noted(session, make_deps, source, transcript, task_21):
    deps = make_deps(result(edits=[{'task_number': 'И-021', 'change': 'выполнено',
                                    'quote': 'И-021 — выполнено', 'timecode': None}]))
    rep = extract_source(session, source, transcript, deps)
    assert session.scalars(select(TaskEdit)).one().timecode is None
    assert 'правка к И-021: таймкод не указан' in rep.notes
