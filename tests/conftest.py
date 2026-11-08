import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from planerka.core.directory import load_diminutives, load_directory, name_variants
from planerka.core.numbering import next_task_number
from planerka.core.statuses import TaskStatus
from planerka.db.models import Employee, Source, Task
from planerka.masking.masker import Masker
from planerka.masking.names import NatashaNameFinder, SurnameEdits
from planerka.masking.tokens import TokenStore

ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def test_engine():
    url = os.environ.get("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST не задан")
    cfg = _alembic_config(url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(test_engine):
    """Сессия в внешней транзакции: после теста всё откатывается."""
    connection = test_engine.connect()
    outer = connection.begin()
    sess = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def task(session):
    """Задача в статусе «на подтверждении» с номером из последовательности."""
    t = Task(
        number=next_task_number(session),
        title="Подготовить прайс-лист",
        type="задача",
        priority="обычный",
        source_id=None,
        status=TaskStatus.CANDIDATE.value,
    )
    session.add(t)
    session.flush()
    return t


@pytest.fixture
def fresh_sequence(session):
    """Последовательность номеров с нуля (ALTER откатывается вместе с тестом)."""
    session.execute(text("ALTER SEQUENCE task_number_seq RESTART WITH 1"))


# Синтетический справочник: порядок задаёт постоянные токены P1, P2, P3.
EMPLOYEES = ['Смирнов Александр', 'Кузнецова Ольга', 'Попов Александр']


@pytest.fixture(scope='session')
def names():
    """Natasha загружается один раз на прогон."""
    return NatashaNameFinder()


@pytest.fixture
def sources(session):
    """Источники с явными id 0..31: тесты маскируют с source_id из этого диапазона."""
    for i in range(32):
        session.add(Source(id=i, kind='запись', path=f'synthetic/{i}.mp3',
                           sha256=f'{i:064d}', status='получено'))
    session.flush()


@pytest.fixture
def employees(session):
    dim = load_diminutives()
    rows = [Employee(full_name=n, name_variants=name_variants(n, dim)) for n in EMPLOYEES]
    session.add_all(rows)
    session.flush()
    return rows


@pytest.fixture
def directory(session, employees):
    return load_directory(session)


@pytest.fixture
def tokens(session, sources, employees):
    store = TokenStore(session)
    for e in employees:  # Смирнов=P1, Кузнецова=P2, Попов=P3
        store.employee(e.id)
    return store


@pytest.fixture
def masker(session, directory, tokens, names):
    return Masker(directory, tokens, names, SurnameEdits.from_settings(session))
