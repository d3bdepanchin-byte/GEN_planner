import pytest

from planerka.cli import build_parser, main


def test_parser_knows_all_commands():
    p = build_parser()
    assert p.parse_args(['worker']).command == 'worker'
    assert p.parse_args(['reprocess', '7']).source_id == 7
    assert p.parse_args(['import-employees', 'staff.xlsx']).path.name == 'staff.xlsx'
    args = p.parse_args(['import-registry', 'reg.xlsx', '--closed'])
    assert (args.path.name, args.closed) == ('reg.xlsx', True)
    assert p.parse_args(['import-registry', 'reg.xlsx']).closed is False
    assert p.parse_args(['backup']).command == 'backup'
    assert p.parse_args(['restore-check', 'b.dump.gpg']).path.name == 'b.dump.gpg'
    args = p.parse_args(['requeue', '5', '--as-planerka'])
    assert (args.source_id, args.as_planerka) == (5, True)
    assert p.parse_args(['requeue', '5']).as_planerka is False


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        main(['nonsense'])


def test_backup_without_passphrase_exits_2(monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://u:p@db/planerka')
    monkeypatch.delenv('BACKUP_PASSPHRASE', raising=False)
    assert main(['backup']) == 2
    assert 'BACKUP_PASSPHRASE' in capsys.readouterr().err


def test_backup_and_restore_check_call_module(monkeypatch, capsys, tmp_path):
    import planerka.backup as backup
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://u:p@db/planerka')
    monkeypatch.setattr(backup, 'make_backup', lambda env, now: [tmp_path / 'planerka_x.dump.gpg'])
    monkeypatch.setattr(backup, 'check_restore', lambda path, env, url: {'task': 3})
    assert main(['backup']) == 0
    assert 'planerka_x.dump.gpg' in capsys.readouterr().out
    assert main(['restore-check', 'b.dump.gpg']) == 0
    assert 'task: 3' in capsys.readouterr().out
