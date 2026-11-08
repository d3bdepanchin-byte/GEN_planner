from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from planerka.mail import imap, smtp
from planerka.mail.base import MailError
from planerka.mail.imap import ImapServiceInbox, imap_utf7_encode, parse_message
from planerka.mail.smtp import SmtpMailer
from planerka.settings import Env


def env(tmp_path, **kw):
    return Env(database_url='x', service_mailbox='planerka@example.test', smtp_host='smtp.example.test',
               smtp_port=465, smtp_user='u', smtp_password='p', imap_host='imap.example.test',
               imap_user='u', imap_password='p', data_dir=tmp_path, **kw)


def test_utf7_done_folder():
    assert imap_utf7_encode('Обработано') == '&BB4EMQRABDAEMQQ+BEIEMAQ9BD4-'


def test_utf7_ascii_and_ampersand():
    assert imap_utf7_encode('INBOX') == 'INBOX'
    assert imap_utf7_encode('A&B') == 'A&-B'
    out = imap_utf7_encode('Файл/1')
    assert out.startswith('&') and out.endswith('-/1') and ',' not in out[:-3]


class FakeSMTP:
    instances = []

    def __init__(self, host, port, *a, **k):
        self.kind, self.port, self.calls, self.msg = type(self).__name__, port, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, **k): self.calls.append('starttls')
    def login(self, u, p): self.calls.append('login')
    def send_message(self, msg): self.msg = msg


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.mark.parametrize('port, kind, tls', [(465, 'FakeSMTPSSL', False), (587, 'FakeSMTP', True)])
def test_smtp_ssl_or_starttls(monkeypatch, tmp_path, port, kind, tls):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp.smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(smtp.smtplib, 'SMTP_SSL', FakeSMTPSSL)
    e = env(tmp_path)
    e.smtp_port = port
    f = tmp_path / 'уточнение_1_2.xlsx'
    f.write_bytes(b'data')
    mid = SmtpMailer(e).send('a@example.test', 'Тема', 'Текст', [f], in_reply_to='<x@y>')
    inst = FakeSMTP.instances[-1]
    assert inst.kind == kind and ('starttls' in inst.calls) is tls and 'login' in inst.calls
    msg = inst.msg
    assert msg['Message-ID'] == mid and mid.endswith('@example.test>')
    assert msg['In-Reply-To'] == '<x@y>' and msg['From'] == 'planerka@example.test'
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed['Subject'] == 'Тема'
    assert [p.get_filename() for p in parsed.iter_attachments()] == ['уточнение_1_2.xlsx']
    assert parsed.get_body().get_content().strip() == 'Текст'


def raw_mail():
    m = EmailMessage()
    m['From'] = 'Смирнов <smirnov@example.test>'
    m['Subject'] = 'Re: Уточнение'
    m['Message-ID'] = '<m1@example.test>'
    m['Auto-Submitted'] = 'no'
    m.set_content('ответ')
    m.add_attachment(b'1', maintype='application', subtype='octet-stream', filename='ответ ../x.xlsx')
    m.add_attachment(b'2', maintype='application', subtype='octet-stream', filename='ответ ../x.xlsx')
    return m.as_bytes()


def test_parse_message_saves_sanitized_attachments(tmp_path):
    mail = parse_message(raw_mail(), tmp_path)
    assert mail.message_id == '<m1@example.test>' and mail.subject == 'Re: Уточнение'
    assert mail.headers['Auto-Submitted'] == 'no'
    assert 'smirnov@example.test' in mail.sender
    assert len(mail.attachments) == 2 and len(set(mail.attachments)) == 2
    for p in mail.attachments:
        assert p.parent.parent == tmp_path / 'mail' and len(p.parent.name) == 16
        assert p.parent == mail.attachments[0].parent
        assert '/' not in p.name and p.suffix == '.xlsx' and p.read_bytes() in (b'1', b'2')


class FakeImap:
    """Минимальный IMAP4_SSL: запоминает команды."""
    def __init__(self, move, fail=(), uidplus=False):
        self.move, self.log, self.folders, self.fail, self.uidplus = move, [], [], set(fail), uidplus

    def capability(self):
        caps = 'IMAP4rev1' + (' MOVE' if self.move else '') + (' UIDPLUS' if self.uidplus else '')
        return 'OK', [caps.encode()]
    def select(self, f): self.log.append(('select', f)); return 'OK', [b'1']
    def list(self): return 'OK', [b'(\\HasNoChildren) "/" "INBOX"'] + [
        f'(\\HasNoChildren) "/" "{f}"'.encode() for f in self.folders]
    def create(self, f):
        self.log.append(('create', f))
        if 'create' in self.fail:
            return 'NO', [b'denied']
        self.folders.append(f)
        return 'OK', []
    def uid(self, cmd, *a):
        self.log.append((cmd, *a))
        if cmd in self.fail:
            return 'NO', [b'failed']
        return 'OK', [b'7'] if cmd == 'SEARCH' else []
    def expunge(self): self.log.append(('expunge',)); return 'OK', []
    def logout(self): return 'BYE', []


@pytest.mark.parametrize('move', [True, False])
def test_mark_done_move_or_copy(monkeypatch, tmp_path, move):
    fake = FakeImap(move)
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    fake.login = lambda u, p: ('OK', [])
    ImapServiceInbox(env(tmp_path)).mark_done('<m1@example.test>')
    cmds = [c[0] for c in fake.log]
    assert ('create', '&BB4EMQRABDAEMQQ+BEIEMAQ9BD4-') in fake.log
    if move:
        assert 'MOVE' in cmds and 'COPY' not in cmds
    else:
        assert 'COPY' in cmds and 'STORE' in cmds and 'expunge' in cmds


@pytest.mark.parametrize('reply, expect', [('<x@y>', True), (None, False)])
def test_smtp_reply_auto_headers(monkeypatch, tmp_path, reply, expect):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp.smtplib, 'SMTP_SSL', FakeSMTPSSL)
    SmtpMailer(env(tmp_path)).send('a@example.test', 's', 'b', [], in_reply_to=reply)
    msg = FakeSMTP.instances[-1].msg
    assert (msg['Auto-Submitted'] == 'auto-replied' and msg['Precedence'] == 'auto_reply') is expect
    assert (msg['Auto-Submitted'] is not None) is expect


def test_mark_done_copy_no_keeps_mail(monkeypatch, tmp_path):
    fake = FakeImap(move=False, fail={'COPY'})
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    fake.login = lambda u, p: ('OK', [])
    with pytest.raises(MailError):
        ImapServiceInbox(env(tmp_path)).mark_done('<m1@example.test>')
    cmds = [c[0] for c in fake.log]
    assert 'STORE' not in cmds and 'expunge' not in cmds and 'EXPUNGE' not in cmds


def test_mark_done_move_no_raises(monkeypatch, tmp_path):
    fake = FakeImap(move=True, fail={'MOVE'})
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    fake.login = lambda u, p: ('OK', [])
    with pytest.raises(MailError):
        ImapServiceInbox(env(tmp_path)).mark_done('<m1@example.test>')


def test_mark_done_create_no_raises(monkeypatch, tmp_path):
    fake = FakeImap(move=True, fail={'create'})
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    fake.login = lambda u, p: ('OK', [])
    with pytest.raises(MailError):
        ImapServiceInbox(env(tmp_path)).mark_done('<m1@example.test>')


def test_mark_done_uidplus_expunges_only_ours(monkeypatch, tmp_path):
    fake = FakeImap(move=False, uidplus=True)
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    fake.login = lambda u, p: ('OK', [])
    ImapServiceInbox(env(tmp_path)).mark_done('<m1@example.test>')
    assert ('EXPUNGE', b'7') in fake.log and 'expunge' not in [c[0] for c in fake.log]


def _msg(name, mid):
    m = EmailMessage()
    m['From'] = 'a@example.test'
    m['Subject'] = 's'
    m['Message-ID'] = mid
    m.set_content('x')
    m.add_attachment(b'1', maintype='application', subtype='octet-stream', filename=name)
    return m.as_bytes()


@pytest.mark.parametrize('name', ['д' * 130 + '.xlsx', 'a\x00b.xlsx', '/etc/passwd.xlsx',
                                  '../../x.xlsx'])
def test_hostile_attachment_names(tmp_path, name):
    mail = parse_message(_msg(name, '<h@example.test>'), tmp_path)
    (p,) = mail.attachments
    assert p.suffix == '.xlsx' and p.parent.parent == tmp_path / 'mail'
    assert len(p.name.encode()) <= 120 and '\x00' not in p.name and p.read_bytes() == b'1'


class FetchImap(FakeImap):
    def __init__(self, raws):
        super().__init__(True)
        self.raws = raws
    def uid(self, cmd, *a):
        if cmd == 'SEARCH':
            return 'OK', [b' '.join(str(i + 1).encode() for i in range(len(self.raws)))]
        if cmd == 'FETCH':
            return 'OK', [(b'1 (BODY[] {1}', self.raws[int(a[0]) - 1]), b')']
        return super().uid(cmd, *a)


def test_broken_message_does_not_block_others(monkeypatch, tmp_path):
    good1, good2 = _msg('a.xlsx', '<g1@example.test>'), _msg('b.xlsx', '<g2@example.test>')
    fake = FetchImap([good1, b'\xff\xfe broken', good2])
    fake.login = lambda u, p: ('OK', [])
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    real = imap.parse_message

    def flaky(raw, data_dir):
        if raw.startswith(b'\xff'):
            raise ValueError('boom')
        return real(raw, data_dir)
    monkeypatch.setattr(imap, 'parse_message', flaky)
    got = ImapServiceInbox(env(tmp_path)).fetch_new()
    assert [m.message_id for m in got] == ['<g1@example.test>', '<g2@example.test>']


def test_broken_message_logs_uid(monkeypatch, tmp_path, caplog):
    fake = FetchImap([_msg('a.xlsx', '<g1@example.test>'), b'\xff\xfe broken'])
    fake.login = lambda u, p: ('OK', [])
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    real = imap.parse_message

    def flaky(raw, data_dir):
        if raw.startswith(b'\xff'):
            raise ValueError('boom')
        return real(raw, data_dir)
    monkeypatch.setattr(imap, 'parse_message', flaky)
    caplog.set_level('WARNING')
    ImapServiceInbox(env(tmp_path)).fetch_new()
    assert 'UID 2' in caplog.text and 'ValueError' in caplog.text


def test_fetch_failure_does_not_block_others(monkeypatch, tmp_path, caplog):
    raws = [_msg('a.xlsx', '<g1@example.test>'), b'', _msg('b.xlsx', '<g2@example.test>')]
    fake = FetchImap(raws)
    fake.login = lambda u, p: ('OK', [])
    plain = fake.uid

    def uid(cmd, *a):
        if cmd == 'FETCH' and a[0] == b'2':
            raise imap.imaplib.IMAP4.error('FETCH failed')
        return plain(cmd, *a)
    fake.uid = uid
    monkeypatch.setattr(imap.imaplib, 'IMAP4_SSL', lambda *a, **k: fake)
    caplog.set_level('WARNING')
    got = ImapServiceInbox(env(tmp_path)).fetch_new()
    assert [m.message_id for m in got] == ['<g1@example.test>', '<g2@example.test>']
    assert 'UID 2' in caplog.text
