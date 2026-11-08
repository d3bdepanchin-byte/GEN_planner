import base64
import hashlib
import imaplib
import logging
import re
from email import message_from_bytes, policy
from pathlib import Path

from planerka.mail.base import IncomingMail, MailError
from planerka.settings import Env

log = logging.getLogger(__name__)

MAX_STEM_BYTES = 100


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
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ext.encode()[:10].decode(errors="ignore")
    stem = stem.encode()[:MAX_STEM_BYTES].decode(errors="ignore").strip(" .")
    return (f"{stem or 'attachment'}.{ext}" if ext else stem) or "attachment"


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
                label = uid.decode(errors="replace")
                try:
                    _, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                except imaplib.IMAP4.error as exc:  # сбой на одном письме — остальные читаем
                    log.warning("письмо UID %s не получено: %s", label, type(exc).__name__)
                    continue
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                if raw is None:
                    continue
                try:
                    mail = parse_message(raw, self.env.data_dir)
                except Exception as exc:  # одно битое письмо не должно блокировать ящик
                    log.warning("письмо UID %s пропущено при разборе: %s", label,
                                type(exc).__name__)
                    continue
                self._uids[mail.message_id] = uid
                result.append(mail)
            log.info("служебный ящик: писем %d", len(result))
            return result
        finally:
            conn.logout()

    @staticmethod
    def _ok(result, what: str) -> None:
        if result[0] != "OK":
            raise MailError(f"IMAP: {what} не выполнено")

    def _ensure_folder(self, conn: imaplib.IMAP4_SSL, folder: str) -> None:
        status, listing = conn.list()
        self._ok((status,), "LIST")
        names = [(line.decode() if isinstance(line, bytes) else str(line)) for line in listing or []]
        if not any(n.rstrip('"').endswith(folder) for n in names):
            self._ok(conn.create(folder), "CREATE")

    def mark_done(self, message_id: str) -> None:
        """Перекладывает письмо в done_folder. Любой отказ сервера — MailError, письмо остаётся."""
        folder = imap_utf7_encode(self.done_folder)
        conn = self._connect()
        try:
            self._ensure_folder(conn, folder)
            self._ok(conn.select("INBOX"), "SELECT")
            uid = self._uids.get(message_id)
            if uid is None:
                status, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", _quote(message_id))
                self._ok((status,), "SEARCH")
                found = (data[0] or b"").split()
                if not found:
                    return
                uid = found[0]
            status, caps_raw = conn.capability()
            self._ok((status,), "CAPABILITY")
            caps = " ".join(c.decode() if isinstance(c, bytes) else str(c)
                            for c in caps_raw).upper().split()
            if "MOVE" in caps:
                self._ok(conn.uid("MOVE", uid, _quote(folder)), "MOVE")
            else:
                self._ok(conn.uid("COPY", uid, _quote(folder)), "COPY")
                self._ok(conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)"), "STORE")
                if "UIDPLUS" in caps:  # убираем только своё письмо, чужие \Deleted не трогаем
                    self._ok(conn.uid("EXPUNGE", uid), "EXPUNGE")
                else:
                    self._ok(conn.expunge(), "EXPUNGE")
            self._uids.pop(message_id, None)
        finally:
            conn.logout()
