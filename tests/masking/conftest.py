from pathlib import Path

import pytest

from planerka.core.directory import load_diminutives, load_directory, name_variants
from planerka.db.models import Employee, Source
from planerka.masking.masker import Masker
from planerka.masking.names import NatashaNameFinder, SurnameEdits
from planerka.masking.tokens import TokenStore

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'

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


def _lines(name: str) -> list[str]:
    """Строки файла без пустых и комментариев «#»."""
    text = (FIXTURES / name).read_text(encoding='utf-8')
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith('#')]


@pytest.fixture(scope='session')
def reference_sentences():
    """30 синтетических фраз со всеми видами ПДн."""
    return _lines('pii_reference.txt')


@pytest.fixture(scope='session')
def pii_strings():
    """ПДн из эталонных фраз: ни одна не должна остаться в замаскированном тексте."""
    return _lines('pii_reference_strings.txt')
