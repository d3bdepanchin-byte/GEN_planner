import pytest

from planerka.mail.imap import ImapServiceInbox
from planerka.mail.smtp import SmtpMailer
from planerka.settings import load_env


@pytest.fixture
def env():
    return load_env()


@pytest.mark.mail
def test_live_roundtrip(env):
    mid = SmtpMailer(env).send(env.service_mailbox, 'проверка', 'тест', [])
    inbox = ImapServiceInbox(env)
    assert any(m.message_id == mid for m in inbox.fetch_new())
    inbox.mark_done(mid)
