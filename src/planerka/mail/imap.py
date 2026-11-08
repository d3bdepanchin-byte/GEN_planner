import base64
import hashlib
import imaplib
import logging
import re
from email import message_from_bytes, policy
from pathlib import Path

from planerka.mail.base import IncomingMail
from planerka.settings import Env

log = logging.getLogger(__name__)


def imap_utf7_encode(text: str) -> str:
    """Имя папки в модифицированном UTF-7 (RFC 3501, 5.1.3); imaplib этого не умеет."""
    out: list[str] = []
    run = ""

    def flush() -> None:
        nonlocal run
        if run:
            b64 = base64.b64encode(run.encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            run = ""

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run += ch
    flush()
    return "".join(out)


def _sanitize(name: str) -> str:
    name = re.sub(r"[^\w.\- ()]", "_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip(" .")
    return name or "attachment"


def _unique(directory: Path, name: str) -> Path:
    path = directory / name
    n = 1
    while path.exists():
        path = directory / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return path


def parse_message(raw: bytes, data_dir: Path) -> IncomingMail:
    """Письмо -> IncomingMail; вложения сохраняются в data_dir/mail/<sha256(message_id)[:16]>/."""
    msg = message_from_bytes(raw, policy=policy.default)
    message_id = str(msg["Message-ID"] or "").strip()
    if not message_id:  # без идентификатора письмо не найти повторно; берём отпечаток тела
        message_id = f"<nomid-{hashlib.sha256(raw).hexdigest()[:16]}@local>"
    directory = Path(data_dir) / "mail" / hashlib.sha256(message_id.encode()).hexdigest()[:16]
    saved: list[Path] = []
    for part in msg.iter_attachments():
        filename = part.get_filename()  # RFC 2047/2231 разворачивает policy.default
        if not filename:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        path = _unique(directory, _sanitize(filename))
        path.write_bytes(part.get_payload(decode=True) or b"")
        saved.append(path)
    headers = {k: str(v) for k, v in msg.items()}
    return IncomingMail(message_id=message_id, sender=str(msg["From"] or ""),
                        subject=str(msg["Subject"] or ""), headers=headers, attachments=saved)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapServiceInbox:
    """Только служебный ящик (Д-75): обработанные письма уходят в папку done_folder."""

    def __init__(self, env: Env, done_folder: str = "Обработано"):
        self.env = env
        self.done_folder = done_folder
        self._uids: dict[str, bytes] = {}

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.env.imap_host, self.env.imap_port)
        conn.login(self.env.imap_user, self.env.imap_password)
        return conn

    def fetch_new(self) -> list[IncomingMail]:
        conn = self._connect()
        try:
            conn.select("INBOX")
            _, data = conn.uid("SEARCH", None, "ALL")
            result: list[IncomingMail] = []
            for uid in (data[0] or b"").split():
                _, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw is None:
                    continue
                mail = parse_message(raw, self.env.data_dir)
                self._uids[mail.message_id] = uid
                result.append(mail)
            log.info("служебный ящик: писем %d", len(result))
            return result
        finally:
            conn.logout()

    def _ensure_folder(self, conn: imaplib.IMAP4_SSL, folder: str) -> None:
        _, listing = conn.list()
        names = [(line.decode() if isinstance(line, bytes) else str(line)) for line in listing or []]
        if not any(n.rstrip('"').endswith(folder) for n in names):
            conn.create(folder)

    def mark_done(self, message_id: str) -> None:
        folder = imap_utf7_encode(self.done_folder)
        conn = self._connect()
        try:
            self._ensure_folder(conn, folder)
            conn.select("INBOX")
            uid = self._uids.get(message_id)
            if uid is None:
                _, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", _quote(message_id))
                found = (data[0] or b"").split()
                if not found:
                    return
                uid = found[0]
            caps = " ".join(c.decode() if isinstance(c, bytes) else str(c)
                            for c in conn.capability()[1]).upper()
            if "MOVE" in caps.split():
                conn.uid("MOVE", uid, _quote(folder))
            else:
                conn.uid("COPY", uid, _quote(folder))
                conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                conn.expunge()
            self._uids.pop(message_id, None)
        finally:
            conn.logout()
