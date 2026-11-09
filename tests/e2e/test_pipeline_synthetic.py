"""Запись планёрки → кандидаты → файлы исполнителей → файл владельца → задачи «в работе».

Только фейки: распознавание, LLM, почта. Люди изображаются тестом: он берёт файлы, которые
отправила система, заполняет и возвращает письмами.
"""
import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from planerka.asr.fake import FakeTranscriber
from planerka.core.directory import load_directory
from planerka.core.registry_import import RegistryMapping, import_registry
from planerka.db.models import Employee, History, Task
from planerka.llm.fake import FakeLLM
from planerka.mail.base import IncomingMail
from planerka.pipeline.jobs import enqueue
from planerka.settings import set_setting
from samples import candidate, llm_result, transcript

PILOT_LAST_NUMBER = 72
MAPPING = RegistryMapping.load()
PILOT_HEADER = ['Номер', 'Задача', 'Тип', 'Приоритет', 'Цель 2027', 'Ответственный', 'Срок',
                'Дата контроля', 'Критерий выполнения', 'Статус']

E2E_TRANSCRIPT = transcript(
    'Начинаем планёрку, тема — прайс-лист, склад и стеллажи.',
    'Фиксируем итоги. Ставим задачу: подготовить прайс-лист, ответственный — Смирнов.',
    'Ставим задачу: сверить остатки на складе.',
    'Ставим задачу: заказать стеллажи, ответственный — Попов.',
)
E2E_LLM = llm_result([
    candidate(),
    candidate(title='Сверить остатки на складе', owner=None, timecode='[01:00]',
              quote='Ставим задачу: сверить остатки на складе.'),
    candidate(title='Заказать стеллажи', owner='[P3]', timecode='[01:30]',
              quote='Ставим задачу: заказать стеллажи, ответственный — [P3]'),
])
NO_OWNER_TITLE = 'Сверить остатки на складе'


@pytest.fixture
def e2e_deps(make_deps):
    return make_deps(E2E_TRANSCRIPT, E2E_LLM)


@pytest.fixture
def pilot_registry(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(PILOT_HEADER)
    ws.append(['И-070', 'Обновить прайс пилота', 'поручение', 'B', None, 'Смирнов Александр',
               None, None, None, 'в работе'])
    ws.append([PILOT_LAST_NUMBER, 'Инвентаризация пилота', 'регламент', None, None,
               'Попов Александр', None, None, None, 'в работе'])
    path = tmp_path / 'pilot.xlsx'
    wb.save(path)
    return path


@pytest.fixture
def e2e_ready(session, employees, host, executor, registry_owner):
    """Ведущий, два исполнителя с e-mail, владелец реестра; приёмник без окна ожидания."""
    employees[2].email = 'popov@example.test'
    set_setting(session, 'intake.stability_window_sec', 0, 'test')
    session.flush()


def drop_recording(env, audio: Path, name: str = 'планёрка.mp3') -> None:
    env.inbox_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(audio, env.inbox_dir / name)


def _files_to(deps, address: str) -> list[Path]:
    return [Path(a) for m in deps.mailer.sent if m['to'] == address for a in m['attachments']]


def _fill(path: Path, out: Path, edit_row) -> Path:
    """Открывает файл системы, вызывает edit_row(header, row_cells) для каждой строки задач."""
    wb = load_workbook(path)
    ws = wb['Задачи']
    header = {c.value: c.column for c in ws[1]}
    for row in ws.iter_rows(min_row=2):
        if row[0].value:
            edit_row(header, {name: ws.cell(row[0].row, col) for name, col in header.items()})
    wb.save(out)
    return out


def _return_mail(deps, sender: str, attachment: Path, n: int) -> None:
    deps.inbox.messages.append(IncomingMail(f'<return-{n}@example.test>', sender,
                                            'Re: файл на уточнение', attachments=[attachment]))


def return_executor_files(deps, tmp_path: Path, decision: str) -> int:
    """Каждый исполнитель возвращает свой файл с одним и тем же решением."""
    returned = 0
    for address in ('smirnov@example.test', 'popov@example.test'):
        for i, src in enumerate(_files_to(deps, address)):
            out = _fill(src, tmp_path / f'back_{returned}.xlsx',
                        lambda h, c: setattr(c['Решение'], 'value', decision))
            _return_mail(deps, address, out, returned)
            returned += 1
    return returned


def return_owner_file(deps, tmp_path: Path, owner_email: str, decision: str,
                      assign: str) -> None:
    """Владелец реестра утверждает всё; где нет ответственного — назначает `assign`."""
    def edit(h, c):
        c['Решение'].value = decision
        if not c['Ответственный'].value:
            c['Ответственный'].value = assign
    src = _files_to(deps, owner_email)[-1]
    _return_mail(deps, owner_email, _fill(src, tmp_path / 'owner_back.xlsx', edit), 100)


def history_states(session, task: Task) -> set[str]:
    rows = session.scalars(select(History).where(History.task_id == task.id,
                                                 History.event == 'status'))
    return {s for h in rows for s in (h.old, h.new) if s}


def _poll_and_digest(session, deps, run_until_idle) -> None:
    enqueue(session, 'poll_mail', {})
    enqueue(session, 'owner_digest', {})
    run_until_idle(session, deps)


def test_planerka_to_approved_tasks(session, e2e_deps, e2e_ready, pilot_registry, audio,
                                    run_until_idle, tmp_path, fresh_sequence):
    env = e2e_deps.env
    import_registry(session, pilot_registry, MAPPING)  # последний номер пилота — 72
    drop_recording(env, audio)
    for _ in range(2):  # первый проход наблюдает файл, второй — принимает
        enqueue(session, 'scan_inbox', {})
        run_until_idle(session, e2e_deps)

    assert [m['to'] for m in e2e_deps.mailer.sent].count('smirnov@example.test') == 1
    assert [m['to'] for m in e2e_deps.mailer.sent].count('popov@example.test') == 1
    new = session.scalars(select(Task).where(Task.number > PILOT_LAST_NUMBER)
                          .order_by(Task.number)).all()
    assert [t.number for t in new] == [73, 74, 75]
    assert {t.status for t in new} == {'на подтверждении'}  # правило 3: сами не стали рабочими

    assert return_executor_files(e2e_deps, tmp_path, decision='подтверждаю') == 2
    _poll_and_digest(session, e2e_deps, run_until_idle)
    assert {t.status for t in new if t.owner_id} == {'подтверждена исполнителем'}

    return_owner_file(e2e_deps, tmp_path, 'owner@example.test', decision='утверждаю',
                      assign='Кузнецова Ольга')
    _poll_and_digest(session, e2e_deps, run_until_idle)

    tasks = session.scalars(select(Task).where(Task.status == 'в работе',
                                               Task.number > PILOT_LAST_NUMBER)
                            .order_by(Task.number)).all()
    assert [t.number for t in tasks] == [73, 74, 75]
    assert min(t.number for t in tasks) == 73
    assert all(t.owner_id for t in tasks)

    directory = load_directory(session)
    assert e2e_deps.llm.requests
    assert not any(v.lower() in r.lower() for r in e2e_deps.llm.requests
                   for e in directory for v in e.variants)

    for t in tasks:
        if t.title == NO_OWNER_TITLE:  # исполнителя не было: владелец назначил и утвердил сам
            assert history_states(session, t) == {'на подтверждении', 'в работе'}
        else:
            assert history_states(session, t) == {'на подтверждении', 'подтверждена исполнителем',
                                                   'в работе'}
