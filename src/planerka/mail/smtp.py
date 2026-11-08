import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from planerka.mail.base import AUTO_HEADERS
from planerka.settings import Env

log = logging.getLogger(__name__)


class SmtpMailer:
    """Отправка со служебного ящика. Порт 465 — SSL, иначе (587) — STARTTLS."""

    def __init__(self, env: Env):
        self.env = env

    def send(self, to: str, subject: str, body: str, attachments: list[Path],
             in_reply_to: str | None = None) -> str:
        env = self.env
        msg = EmailMessage()
        msg["From"] = env.service_mailbox
        msg["To"] = to
        msg["Subject"] = subject
        message_id = make_msgid(domain=env.service_mailbox.rpartition("@")[2] or None)
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
            for name, value in AUTO_HEADERS.items():
                msg[name] = value
        msg.set_content(body)
        for path in attachments:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            maintype, _, subtype = mime.partition("/")
            msg.add_attachment(Path(path).read_bytes(), maintype=maintype, subtype=subtype,
                               filename=path.name)  # кириллица кодируется по RFC 2231
        if env.smtp_port == 465:
            conn = smtplib.SMTP_SSL(env.smtp_host, env.smtp_port, timeout=60)
        else:
            conn = smtplib.SMTP(env.smtp_host, env.smtp_port, timeout=60)
        with conn as smtp:
            if env.smtp_port != 465:
                smtp.starttls()
            if env.smtp_user:
                smtp.login(env.smtp_user, env.smtp_password)
            smtp.send_message(msg)
        log.info("письмо отправлено: вложений %d", len(attachments))
        return message_id
