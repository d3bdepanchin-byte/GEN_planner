"""Команда `planerka`: воркер, переобработка, импорт справочника и реестра, бэкапы.

Вывод — только счётчики, номера и пути: без ФИО и текста записей.
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from planerka.settings import Env, load_env


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='planerka', description='Планёрка: записи встреч в задачи')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('worker', help='конвейер: приёмник, распознавание, разбор, почта')
    rp = sub.add_parser('reprocess', help='переобработка записи без новых задач (Д-51)')
    rp.add_argument('source_id', type=int)
    rq = sub.add_parser('requeue', help='повтор вручную по статусу источника')
    rq.add_argument('source_id', type=int)
    rq.add_argument('--as-planerka', action='store_true',
                    help='запись без фразы открытия разобрать как планёрку')
    ie = sub.add_parser('import-employees', help='справочник сотрудников из xlsx')
    ie.add_argument('path', type=Path)
    ir = sub.add_parser('import-registry', help='реестр пилота из xlsx')
    ir.add_argument('path', type=Path)
    ir.add_argument('--closed', action='store_true', help='закрытый реестр')
    sub.add_parser('backup', help='зашифрованный бэкап БД и таблицы токенов')
    rc = sub.add_parser('restore-check', help='проверка восстановления из бэкапа')
    rc.add_argument('path', type=Path)
    rc.add_argument('--scratch-url', default='', help='временная БД (по умолчанию planerka_scratch)')
    st = sub.add_parser('setting', help='настройка: без значения печатает текущее')
    st.add_argument('key')
    st.add_argument('value', nargs='?', help='число, строка, true/false или список [1, 2]')
    sub.add_parser('test-alert', help='тестовый алерт разработчику и сисадмину')
    return p


def _print_import(report) -> None:
    print(f'создано {report.created}, обновлено {report.updated}, '
          f'замечаний {len(report.problems)}')
    for line in report.problems:  # только номера строк и виды проблем
        print(f'- {line}')


def _worker(env: Env, args) -> int:
    from planerka.pipeline.worker import run_worker
    run_worker(env)
    return 0


def _reprocess(env: Env, args) -> int:
    from planerka.db import session_scope
    from planerka.pipeline.deps import build_deps
    from planerka.pipeline.reprocess import reprocess
    with session_scope(env.database_url) as session:
        out = reprocess(session, args.source_id, build_deps(session, env))
    print(out)
    return 0


def _requeue(env: Env, args) -> int:
    from planerka.db import session_scope
    from planerka.pipeline.requeue import RequeueRefused, requeue
    try:
        with session_scope(env.database_url) as session:
            kind = requeue(session, args.source_id, args.as_planerka)
    except RequeueRefused as e:
        print(e, file=sys.stderr)
        return 2
    print(f'источник {args.source_id}: в очереди {kind}')
    return 0


def _import_employees(env: Env, args) -> int:
    from planerka.core.directory import import_employees, load_diminutives
    from planerka.db import session_scope
    with session_scope(env.database_url) as session:
        _print_import(import_employees(session, args.path, load_diminutives()))
    return 0


def _import_registry(env: Env, args) -> int:
    from planerka.core.registry_import import RegistryMapping, import_registry
    from planerka.db import session_scope
    registry = 'закрытый' if args.closed else 'общий'
    with session_scope(env.database_url) as session:
        _print_import(import_registry(session, args.path, RegistryMapping.load(), registry))
    return 0


def _backup(env: Env, args) -> int:
    from planerka.backup import BackupError, make_backup
    try:
        paths = make_backup(env, datetime.now(timezone.utc))
    except BackupError as e:
        print(e, file=sys.stderr)
        return 2
    for path in paths:
        print(path)
    return 0


def _restore_check(env: Env, args) -> int:
    from planerka.backup import BackupError, check_restore
    try:
        counts = check_restore(args.path, env, args.scratch_url)
    except BackupError as e:
        print(e, file=sys.stderr)
        return 2
    for table, rows in counts.items():
        print(f'{table}: {rows}')
    return 0


def _parse_value(raw: str):
    """Значение как скаляр или список TOML; не разобралось — обычная строка."""
    import tomllib
    try:
        return tomllib.loads(f'v = {raw}')['v']
    except tomllib.TOMLDecodeError:
        return raw


def _setting(env: Env, args) -> int:
    import json
    from planerka.db import session_scope
    from planerka.settings import (
        SettingTypeError, UnknownSetting, coerce_setting, get_setting, set_setting,
    )
    try:
        value = None if args.value is None else coerce_setting(args.key, _parse_value(args.value))
        with session_scope(env.database_url) as session:
            if args.value is None:
                print(json.dumps(get_setting(session, args.key), ensure_ascii=False))
            else:
                set_setting(session, args.key, value, 'cli')
                print(f'{args.key}: задано')
    except UnknownSetting:
        print(f'неизвестная настройка: {args.key}', file=sys.stderr)
        return 2
    except SettingTypeError as e:
        print(f'{e}; значение не задано', file=sys.stderr)
        return 2
    return 0


def _test_alert(env: Env, args) -> int:
    from planerka.core.directory import load_directory
    from planerka.db import session_scope
    from planerka.mail.smtp import SmtpMailer
    from planerka.notify import send_alert_safe
    from planerka.settings import get_setting
    with session_scope(env.database_url) as session:
        to = [a for a in (get_setting(session, 'alerts.developer_email'),
                          get_setting(session, 'alerts.sysadmin_email')) if a]
        if not to:
            print('alerts.developer_email и alerts.sysadmin_email не заданы', file=sys.stderr)
            return 2
        ok = send_alert_safe(SmtpMailer(env), to, 'test', None,
                             'проверка канала алертов', load_directory(session))
    print(f'отправлено адресатам: {len(to)}' if ok else 'алерт не отправлен, см. журнал',
          file=None if ok else sys.stderr)
    return 0 if ok else 2


COMMANDS = {
    'setting': _setting, 'test-alert': _test_alert,
    'worker': _worker, 'reprocess': _reprocess, 'requeue': _requeue,
    'import-employees': _import_employees,
    'import-registry': _import_registry, 'backup': _backup, 'restore-check': _restore_check,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    return COMMANDS[args.command](load_env(), args)


if __name__ == '__main__':
    sys.exit(main())
