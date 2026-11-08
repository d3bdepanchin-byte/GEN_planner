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


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        main(['nonsense'])
