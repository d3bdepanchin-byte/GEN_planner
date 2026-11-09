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


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r'\\(.)', r'\1', value[1:-1])
    return value


# Ответ LIST: (флаги) "разделитель"|NIL имя — имя в кавычках или атомом.
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_NAMESPACE_RE = re.compile(r'^\(\(("(?:[^"\\]|\\.)*")')


def _parse_list_entry(item) -> tuple[str, str | None] | None:
    """(имя папки, разделитель) из строки ответа LIST; имя-литерал приходит кортежем."""
    if isinstance(item, tuple):
        head = item[0].decode(errors="replace") if isinstance(item[0], bytes) else str(item[0])
        name = item[1].decode(errors="replace") if isinstance(item[1], bytes) else str(item[1])
        m = _LIST_RE.match(re.sub(r"\{\d+\}$", '""', head.strip()))
        return (name, _unquote(m.group("delim")) if m and m.group("delim") != "NIL" else None)
    line = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
    m = _LIST_RE.match(line.strip())
    if m is None:
        return None
    delim = None if m.group("delim") == "NIL" else _unquote(m.group("delim"))
    return _unquote(m.group("name").strip()), delim


class ImapServiceInbox:
    """Только служебный ящик (Д-75): обработанные письма уходят в папку done_folder."""

    def __init__(self, env: Env, done_folder: str = "Обработано", max_parse_failures: int = 5):
        self.env = env
        self.done_folder = done_folder
        self.max_parse_failures = max_parse_failures
        self._uids: dict[str, bytes] = {}
        self._parse_failures: dict[bytes, int] = {}  # UID → неудач разбора подряд
        self._set_aside: list[str] = []  # «UID n, отпечаток …» — для алерта, без ПДн

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
                    self._parse_failed(conn, uid, raw)
                    continue
                self._parse_failures.pop(uid, None)
                self._uids[mail.message_id] = uid
                result.append(mail)
            log.info("служебный ящик: писем %d", len(result))
            return result
        finally:
            conn.logout()

    def _parse_failed(self, conn: imaplib.IMAP4_SSL, uid: bytes, raw: bytes) -> None:
        """После max_parse_failures неудач подряд письмо уходит в done_folder без ответа."""
        count = self._parse_failures.get(uid, 0) + 1
        self._parse_failures[uid] = count
        if count < self.max_parse_failures:
            return
        digest = hashlib.sha256(raw).hexdigest()[:12]
        label = f"UID {uid.decode(errors='replace')}, отпечаток {digest}"
        try:
            caps = self._capabilities(conn)
            folder = self._ensure_folder(conn, imap_utf7_encode(self.done_folder), caps)
            self._move(conn, uid, folder, caps)
        except (MailError, imaplib.IMAP4.error) as exc:  # не убралось — попробуем в следующий раз
            log.error("письмо %s не убрано из входящих: %s", label, type(exc).__name__)
            return
        self._parse_failures.pop(uid, None)
        self._set_aside.append(label)

    def take_set_aside(self) -> list[str]:
        """Письма, убранные без разбора с прошлого вызова (для алерта); список очищается."""
        out, self._set_aside = self._set_aside, []
        return out

    @staticmethod
    def _ok(result, what: str) -> None:
        if result[0] != "OK":
            raise MailError(f"IMAP: {what} не выполнено")

    @staticmethod
    def _capabilities(conn: imaplib.IMAP4_SSL) -> set[str]:
        status, caps_raw = conn.capability()
        ImapServiceInbox._ok((status,), "CAPABILITY")
        return set(" ".join(c.decode() if isinstance(c, bytes) else str(c)
                            for c in caps_raw).upper().split())

    @staticmethod
    def _personal_prefix(conn: imaplib.IMAP4_SSL, caps: set[str]) -> str:
        """Префикс личного пространства имён (RFC 2342), напр. «INBOX.»; нет — пусто."""
        if "NAMESPACE" not in caps:
            return ""
        status, data = conn.namespace()
        if status != "OK" or not data:
            return ""
        raw = data[0].decode(errors="replace") if isinstance(data[0], bytes) else str(data[0])
        m = _NAMESPACE_RE.match(raw)
        return _unquote(m.group(1)) if m else ""

    def _ensure_folder(self, conn: imaplib.IMAP4_SSL, folder: str, caps: set[str]) -> str:
        """Имя папки на сервере: точное совпадение в LIST, с префиксом пространства имён или
        «INBOX<разделитель>»; нет — создаётся (с префиксом, если сервер его требует)."""
        status, listing = conn.list()
        self._ok((status,), "LIST")
        names, delimiters = set(), set()
        for item in listing or []:
            entry = _parse_list_entry(item)
            if entry is not None:
                names.add(entry[0])
                if entry[1]:
                    delimiters.add(entry[1])
        prefix = self._personal_prefix(conn, caps)
        candidates = [prefix + folder] + [f"INBOX{d}{folder}" for d in sorted(delimiters)]
        for name in [folder, *candidates]:
            if name in names:
                return name
        target = prefix + folder
        self._ok(conn.create(target), "CREATE")
        return target

    def mark_done(self, message_id: str) -> None:
        """Перекладывает письмо в done_folder. Любой отказ сервера — MailError, письмо остаётся."""
        conn = self._connect()
        try:
            uid = self._uids.get(message_id)
            caps = self._capabilities(conn)
            folder = self._ensure_folder(conn, imap_utf7_encode(self.done_folder), caps)
            self._ok(conn.select("INBOX"), "SELECT")
            if uid is None:
                status, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", _quote(message_id))
                self._ok((status,), "SEARCH")
                found = (data[0] or b"").split()
                if not found:
                    return
                uid = found[0]
            self._move(conn, uid, folder, caps)
            self._uids.pop(message_id, None)
        finally:
            conn.logout()

    def _move(self, conn: imaplib.IMAP4_SSL, uid: bytes, folder: str, caps: set[str]) -> None:
        if "MOVE" in caps:
            self._ok(conn.uid("MOVE", uid, _quote(folder)), "MOVE")
            return
        self._ok(conn.uid("COPY", uid, _quote(folder)), "COPY")
        self._ok(conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)"), "STORE")
        if "UIDPLUS" in caps:  # убираем только своё письмо, чужие \Deleted не трогаем
            self._ok(conn.uid("EXPUNGE", uid), "EXPUNGE")
        else:
            self._ok(conn.expunge(), "EXPUNGE")
