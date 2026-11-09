"""Зашифрованные бэкапы и проверка восстановления (Д-32, Д-33, Д-80).

pg_dump и gpg работают конвейером: открытого дампа на диске нет. Парольная фраза идёт в gpg
через отдельный канал (fd), не через командную строку и не через окружение. Тексты ошибок
без данных из БД: только имя инструмента и код возврата.
"""
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from planerka.db import session_scope
from planerka.settings import Env, get_setting

log = logging.getLogger(__name__)

SCRATCH_DB = 'planerka_scratch'  # создаётся docker/db-init.sql
FULL_PREFIX = 'planerka'
TOKENS_PREFIX = 'tokens'
STAMP = '%Y%m%d-%H%M%S'
_NAME_RE = re.compile(rf'^({FULL_PREFIX}|{TOKENS_PREFIX})_(\d{{8}}-\d{{6}})\.dump\.gpg$')
GPG = ['gpg', '--batch', '--yes', '--no-tty', '--pinentry-mode', 'loopback']


class BackupError(RuntimeError):
    """Бэкап или проверка не выполнены. Текст без данных из БД: только инструмент и код."""


def _existing_ancestor(p: Path) -> Path:
    p = p.absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def same_device(a: Path, b: Path) -> bool:
    """Каталоги на одном устройстве; несуществующие — по ближайшему существующему родителю."""
    return os.stat(_existing_ancestor(a)).st_dev == os.stat(_existing_ancestor(b)).st_dev


def _pg_args(url: URL) -> tuple[list[str], dict[str, str]]:
    """Параметры подключения для pg_*: пароль — только в окружении дочернего процесса."""
    args = []
    if url.host:
        args += ['-h', url.host]
    if url.port:
        args += ['-p', str(url.port)]
    if url.username:
        args += ['-U', url.username]
    args += ['-d', url.database or '']
    env = dict(os.environ)
    if url.password:
        env['PGPASSWORD'] = url.password
    return args, env


def _passphrase_pipe(passphrase: str) -> int:
    """Читающий конец канала с фразой; пишущий закрыт."""
    r, w = os.pipe()
    with os.fdopen(w, 'w') as f:
        f.write(passphrase + '\n')
    return r


def _run_pipeline(first: list[str], first_env: dict[str, str], second: list[str],
                  passphrase: str, tool_names: tuple[str, str]) -> None:
    """first | second: во второй команде `{fd}` — канал с фразой; stderr не показывается."""
    fd = _passphrase_pipe(passphrase)
    producer = consumer = None
    try:
        cmd2 = [a.replace('{fd}', str(fd)) for a in second]
        producer = subprocess.Popen(first, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=first_env)
        consumer = subprocess.Popen(cmd2, stdin=producer.stdout, stderr=subprocess.DEVNULL,
                                    pass_fds=(fd,))
        producer.stdout.close()
        rc2 = consumer.wait()
        rc1 = producer.wait()
    finally:
        os.close(fd)
        for p in (producer, consumer):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait()
    for name, rc in zip(tool_names, (rc1, rc2)):
        if rc != 0:
            raise BackupError(f'{name} завершился с кодом {rc}')


def _backup_one(url: URL, out: Path, passphrase: str, extra: list[str]) -> None:
    args, child_env = _pg_args(url)
    part = out.with_name(out.name + '.part')
    part.unlink(missing_ok=True)
    gpg = GPG + ['--symmetric', '--cipher-algo', 'AES256', '--compress-level', '0',
                 '--passphrase-fd', '{fd}', '-o', str(part)]
    try:
        _run_pipeline(['pg_dump', '-Fc', *extra, *args], child_env, gpg, passphrase,
                      tool_names=('pg_dump', 'gpg'))
        os.chmod(part, 0o600)
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _remove_old(backup_dir: Path, now: datetime, keep_days: int) -> None:
    border = now.replace(tzinfo=None) - timedelta(days=keep_days)
    for p in backup_dir.iterdir():
        m = _NAME_RE.match(p.name)
        if m and p.is_file() and datetime.strptime(m.group(2), STAMP) < border:
            p.unlink()
            log.info('удалён старый бэкап %s', p.name)


def make_backup(env: Env, now: datetime) -> list[Path]:
    """Полный дамп БД и отдельно таблица token, оба зашифрованы. Старше keep_days — удаляются."""
    if not env.backup_passphrase:
        raise BackupError('BACKUP_PASSPHRASE не задан: бэкап без шифрования не пишем')
    if same_device(env.backup_dir, env.data_dir):
        log.warning('BACKUP_DIR на том же устройстве, что DATA_DIR: при потере диска пропадёт '
                    'и бэкап; нужна копия на другой сервер (Д-80)')
    env.backup_dir.mkdir(parents=True, exist_ok=True)
    url = make_url(env.database_url)
    stamp = now.strftime(STAMP)
    plan = [(FULL_PREFIX, []), (TOKENS_PREFIX, ['-t', 'token'])]
    paths = []
    for prefix, extra in plan:
        out = env.backup_dir / f'{prefix}_{stamp}.dump.gpg'
        _backup_one(url, out, env.backup_passphrase, extra)
        paths.append(out)
        log.info('бэкап записан: %s', out.name)
    with session_scope(env.database_url) as session:
        keep_days = int(get_setting(session, 'backup.keep_days'))
    _remove_old(env.backup_dir, now, keep_days)
    return paths


def default_scratch_url(env: Env) -> str:
    """Временная БД на том же сервере, что и основная."""
    return make_url(env.database_url).set(database=SCRATCH_DB).render_as_string(
        hide_password=False)


def _count_rows(url: URL) -> dict[str, int]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            quote = engine.dialect.identifier_preparer.quote
            tables = conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name")).scalars().all()
            return {t: conn.execute(text(f'SELECT count(*) FROM {quote(t)}')).scalar_one()
                    for t in tables}
    finally:
        engine.dispose()


def check_restore(path: Path, env: Env, scratch_url: str) -> dict[str, int]:
    """Расшифровать, восстановить во временную БД, вернуть число строк по таблицам.

    Только временная БД: если она совпадает с основной — отказ до любых действий.
    Дамп таблицы token восстанавливается без ограничений (внешние ключи ведут на таблицы,
    которых в дампе нет).
    """
    if not env.backup_passphrase:
        raise BackupError('BACKUP_PASSPHRASE не задан: расшифровать нечем')
    target = make_url(scratch_url or default_scratch_url(env))
    main = make_url(env.database_url)
    if (target.database == main.database and target.host == main.host
            and target.port == main.port) or not target.database:
        raise BackupError('проверка восстановления идёт только во временную БД, '
                          'а не в основную')
    args, child_env = _pg_args(target)
    restore = ['pg_restore', '--clean', '--if-exists', '--no-owner', '--no-privileges']
    if path.name.startswith(TOKENS_PREFIX + '_'):
        restore += ['--section=pre-data', '--section=data']
    decrypt = GPG + ['--decrypt', '--passphrase-fd', '{fd}', str(path)]
    fd = _passphrase_pipe(env.backup_passphrase)
    gpg = pg = None
    try:
        gpg = subprocess.Popen([a.replace('{fd}', str(fd)) for a in decrypt],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               pass_fds=(fd,))
        pg = subprocess.Popen([*restore, *args], stdin=gpg.stdout, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=child_env)
        gpg.stdout.close()
        rc_pg = pg.wait()
        rc_gpg = gpg.wait()
    finally:
        os.close(fd)
        for p in (gpg, pg):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait()
    if rc_gpg != 0:
        raise BackupError(f'gpg не расшифровал файл (код {rc_gpg}): неверная фраза или '
                          'повреждённый файл')
    if rc_pg != 0:
        raise BackupError(f'pg_restore завершился с кодом {rc_pg}')
    return _count_rows(target)
