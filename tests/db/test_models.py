from sqlalchemy import inspect, select

from planerka.db.models import Employee, Task


def test_migration_creates_all_tables(session):
    names = set(inspect(session.bind).get_table_names())
    assert {'employee', 'channel_id', 'source', 'token', 'task', 'task_edit',
            'history', 'setting', 'job', 'review_file'} <= names


def test_task_defaults(session, task):
    assert task.registry == 'общий'
    assert task.created_at is not None and task.created_at.tzinfo is not None
    assert task.questions == [] and task.flags == []


def test_list_append_is_persisted(session, task):
    task.flags = [*task.flags, 'нет срока']
    session.flush()
    session.expire(task)
    assert task.flags == ['нет срока']


def test_employee_array(session):
    e = Employee(full_name='Иван Петров', name_variants=['Ваня', 'Иван'])
    session.add(e)
    session.flush()
    session.expire_all()
    assert session.scalars(select(Employee)).one().name_variants == ['Ваня', 'Иван']
