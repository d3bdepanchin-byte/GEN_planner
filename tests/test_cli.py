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


@pytest.fixture
def cli_db(test_engine, monkeypatch):
    import os
    from sqlalchemy import text
    monkeypatch.setenv('DATABASE_URL', os.environ['DATABASE_URL_TEST'])
    yield
    with test_engine.begin() as c:
        c.execute(text("DELETE FROM setting WHERE key IN ('meeting.host_employee_id', "
                       "'registry.closed_access_employee_ids', 'alerts.developer_email', "
                       "'alerts.sysadmin_email')"))


def test_setting_prints_default_and_sets_values(cli_db, capsys):
    assert main(['setting', 'backup.keep_days']) == 0
    assert capsys.readouterr().out.strip() == '30'
    assert main(['setting', 'meeting.host_employee_id', '12']) == 0
    capsys.readouterr()
    main(['setting', 'meeting.host_employee_id'])
    assert capsys.readouterr().out.strip() == '12'
    assert main(['setting', 'registry.closed_access_employee_ids', '[3, 4]']) == 0
    capsys.readouterr()
    main(['setting', 'registry.closed_access_employee_ids'])
    assert capsys.readouterr().out.strip() == '[3, 4]'
    assert main(['setting', 'alerts.developer_email', 'dev@example.test']) == 0  # голая строка


def test_setting_unknown_key_exits_2(cli_db, capsys):
    assert main(['setting', 'no.such.key']) == 2
    assert main(['setting', 'no.such.key', '1']) == 2


def test_test_alert_sends_to_both(cli_db, monkeypatch, capsys):
    from planerka.mail.fake import FakeMailer
    import planerka.mail.smtp as smtp
    mailer = FakeMailer()
    monkeypatch.setattr(smtp, 'SmtpMailer', lambda env: mailer)
    main(['setting', 'alerts.developer_email', 'dev@example.test'])
    main(['setting', 'alerts.sysadmin_email', 'adm@example.test'])
    assert main(['test-alert']) == 0
    assert sorted(m['to'] for m in mailer.sent) == ['adm@example.test', 'dev@example.test']


def test_test_alert_without_addresses_exits_2(cli_db, monkeypatch, capsys):
    from planerka.mail.fake import FakeMailer
    import planerka.mail.smtp as smtp
    monkeypatch.setattr(smtp, 'SmtpMailer', lambda env: FakeMailer())
    assert main(['test-alert']) == 2
