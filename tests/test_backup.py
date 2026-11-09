import os
import subprocess
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import planerka.backup as backup
from planerka.backup import BackupError, check_restore, make_backup, same_device
from planerka.settings import Env

NOW = datetime(2026, 11, 9, 3, 0, 0, tzinfo=timezone.utc)
PASSPHRASE = 'test-passphrase-7'


@pytest.fixture
def db_url(test_engine):
    return os.environ['DATABASE_URL_TEST']


@pytest.fixture
def scratch_url(db_url):
    return make_url(db_url).set(database='planerka_scratch').render_as_string(hide_password=False)


@pytest.fixture
def env(db_url, tmp_path):
    return Env(database_url=db_url, backup_dir=tmp_path / 'backups', data_dir=tmp_path / 'data',
               backup_passphrase=PASSPHRASE)


@pytest.fixture
def db_with_three_tasks(db_url):
    """Данные закоммичены: pg_dump видит только их. После теста удаляются."""
    engine = create_engine(db_url)
    with engine.begin() as c:
        emp = c.execute(text("INSERT INTO employee (full_name, name_variants) "
                             "VALUES ('Бэкапов Тест', '{}') RETURNING id")).scalar_one()
        for n in (9001, 9002, 9003):
            c.execute(text("INSERT INTO task (number, title, status) "
                           "VALUES (:n, 'Задача для бэкапа', 'на подтверждении')"), {'n': n})
        c.execute(text("INSERT INTO token (token, kind, employee_id) "
                       "VALUES ('[P9001]', 'employee', :e)"), {'e': emp})
    yield
    with engine.begin() as c:
        c.execute(text("DELETE FROM token WHERE token = '[P9001]'"))
        c.execute(text("DELETE FROM task WHERE number IN (9001, 9002, 9003)"))
        c.execute(text("DELETE FROM employee WHERE full_name = 'Бэкапов Тест'"))
    engine.dispose()


def _full(env):
    return [p for p in make_backup(env, NOW) if p.name.startswith('planerka_')][0]


def test_backup_is_encrypted(env, db_with_three_tasks):
    paths = make_backup(env, NOW)
    assert paths
    for p in paths:
        data = p.read_bytes()
        assert not data.startswith(b'PGDMP')
        assert 'Задача для бэкапа'.encode() not in data


def test_backup_names_and_no_leftovers(env, db_with_three_tasks):
    names = sorted(p.name for p in make_backup(env, NOW))
    assert names == ['planerka_20261109-030000.dump.gpg', 'tokens_20261109-030000.dump.gpg']
    assert sorted(p.name for p in env.backup_dir.iterdir()) == names


def test_tokens_backed_up_separately(env, db_with_three_tasks):
    assert any(p.name.startswith('tokens_') for p in make_backup(env, NOW))


def test_restore_check_counts_rows(env, db_with_three_tasks, scratch_url):
    counts = check_restore(_full(env), env, scratch_url)
    assert counts['task'] == 3
    assert counts['token'] == 1


def test_restore_check_tokens_file(env, db_with_three_tasks, scratch_url):
    tok = [p for p in make_backup(env, NOW) if p.name.startswith('tokens_')][0]
    assert check_restore(tok, env, scratch_url)['token'] == 1


def test_restore_check_refuses_main_database(env, db_with_three_tasks, db_url):
    full = _full(env)
    with pytest.raises(BackupError, match='основн'):
        check_restore(full, env, db_url)
    with engine_conn(db_url) as c:  # данные основной БД не тронуты
        assert c.execute(text('SELECT count(*) FROM task WHERE number >= 9001')).scalar_one() == 3


def engine_conn(url):
    from contextlib import contextmanager

    @contextmanager
    def cm():
        engine = create_engine(url)
        try:
            with engine.connect() as c:
                yield c
        finally:
            engine.dispose()
    return cm()


def test_restore_check_wrong_passphrase(env, db_with_three_tasks, scratch_url):
    full = _full(env)
    wrong = env.model_copy(update={'backup_passphrase': 'other-passphrase'})
    with pytest.raises(BackupError):
        check_restore(full, wrong, scratch_url)


def test_empty_passphrase_refused(env, db_with_three_tasks):
    bad = env.model_copy(update={'backup_passphrase': ''})
    with pytest.raises(BackupError, match='BACKUP_PASSPHRASE'):
        make_backup(bad, NOW)
    assert not env.backup_dir.exists() or list(env.backup_dir.iterdir()) == []


def test_passphrase_not_in_command_line_or_env(env, db_with_three_tasks, monkeypatch):
    seen = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, args, *a, **kw):
            seen.append((list(args), dict(kw.get('env') or {})))
            super().__init__(args, *a, **kw)
    monkeypatch.setattr(backup.subprocess, 'Popen', Spy)
    make_backup(env, NOW)
    assert seen
    for args, child_env in seen:
        assert PASSPHRASE not in ' '.join(args)
        assert PASSPHRASE not in child_env.values()


def test_old_backups_removed_only_matching_names(env, db_with_three_tasks):
    env.backup_dir.mkdir()
    old = [env.backup_dir / 'planerka_20260101-030000.dump.gpg',
           env.backup_dir / 'tokens_20260101-030000.dump.gpg']
    fresh = env.backup_dir / 'planerka_20261105-030000.dump.gpg'
    foreign = [env.backup_dir / 'notes_20200101-000000.txt',
               env.backup_dir / 'planerka_old.dump.gpg']
    for p in old + [fresh] + foreign:
        p.write_bytes(b'x')
    make_backup(env, NOW)  # keep_days = 30: 01.01 удаляются, 05.11 остаётся
    assert not any(p.exists() for p in old)
    assert fresh.exists() and all(p.exists() for p in foreign)


def test_failed_dump_leaves_no_file(env, db_with_three_tasks):
    bad = env.model_copy(update={'database_url': env.database_url.replace('planerka_test', 'nope')})
    with pytest.raises(BackupError) as err:
        make_backup(bad, NOW)
    assert 'Бэкапов' not in str(err.value)
    assert not env.backup_dir.exists() or list(env.backup_dir.iterdir()) == []


def test_same_device_detected(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    assert same_device(tmp_path / 'a', tmp_path / 'b')


def test_same_device_for_missing_dirs(tmp_path):
    assert same_device(tmp_path / 'no' / 'a', tmp_path / 'b')


def test_same_device_warning_logged(env, db_with_three_tasks, caplog):
    with caplog.at_level('WARNING'):
        make_backup(env, NOW)  # backups и data в одном tmp_path
    assert any('том же' in r.message for r in caplog.records)
