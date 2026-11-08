import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from planerka.db.models import Employee, Source, Task, Token


def test_migration_creates_all_tables(session):
    names = set(inspect(session.bind).get_table_names())
    assert {'employee', 'channel_id', 'source', 'token', 'task', 'task_edit',
            'history', 'setting', 'job', 'review_file'} <= names


def test_task_defaults(session, task):
    assert task.registry == 'общий'
    assert task.created_at is not None and task.created_at.tzinfo is not None
    assert task.questions == [] and task.flags == []


def test_in_place_changes_are_tracked(session, task):
    task.flags.append('нет срока')
    task.questions.append('Кто исполнитель?')
    source = _source(session, 'c' * 64)
    source.flags['k'] = 'v'
    session.flush()
    session.expire_all()
    assert task.flags == ['нет срока']
    assert task.questions == ['Кто исполнитель?']
    assert source.flags == {'k': 'v'}


def test_employee_array(session):
    e = Employee(full_name='Иван Петров', name_variants=['Ваня', 'Иван'])
    session.add(e)
    session.flush()
    session.expire_all()
    assert session.scalars(select(Employee)).one().name_variants == ['Ваня', 'Иван']


def _source(session, sha):
    s = Source(kind='planerka', path=f'{sha}.mp3', sha256=sha, status='new')
    session.add(s)
    session.flush()
    return s


def test_same_external_token_in_two_sources(session):
    a, b = _source(session, 'a' * 64), _source(session, 'b' * 64)
    session.add_all([Token(token='[X1]', kind='external', source_id=a.id),
                     Token(token='[X1]', kind='external', source_id=b.id)])
    session.flush()


def test_duplicate_token_in_one_source_rejected(session):
    a = _source(session, 'a' * 64)
    session.add_all([Token(token='[X1]', kind='external', source_id=a.id),
                     Token(token='[X1]', kind='external', source_id=a.id)])
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_employee_token_rejected(session):
    session.add_all([Token(token='[P1]', kind='employee'),
                     Token(token='[P1]', kind='employee')])
    with pytest.raises(IntegrityError):
        session.flush()


def test_second_permanent_token_for_employee_rejected(session):
    e = Employee(full_name='Иван Петров')
    session.add(e)
    session.flush()
    session.add_all([Token(token='[P1]', kind='employee', employee_id=e.id),
                     Token(token='[P2]', kind='employee', employee_id=e.id)])
    with pytest.raises(IntegrityError):
        session.flush()
