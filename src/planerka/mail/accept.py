"""Приём ответа на файл уточнения (Д-74): проверки, применение, ответ отправителю."""
import hashlib
import logging
from enum import Enum
from pathlib import Path

from email.utils import parseaddr

from sqlalchemy.orm import Session

from planerka.db.models import Employee, ReviewFile
from planerka.mail.base import IncomingMail, Mailer
from planerka.review.executor import (
    ParsedReview, ReviewErrors, ReviewRejected, apply_executor_review, parse_executor_file,
)
from planerka.review.owner import apply_owner_review, parse_owner_file
from planerka.review.xlsx import MetaError, NOT_FROM_SYSTEM, read_meta

log = logging.getLogger(__name__)

AUTOREPLY_PRECEDENCE = {"auto_reply", "bulk", "junk"}
AUTOREPLY_SUBJECTS = ("автоответ", "auto")

HINT_NO_XLSX = ("Во вложении нет файла .xlsx. Верните ответом на письмо файл, "
                "который прислала система, в формате .xlsx.")
HINT_NOT_FROM_SYSTEM = ("Файл не из системы или повреждён. Верните тот файл .xlsx, "
                        "который прислала система, без пересохранения в другом формате.")
REJECT_SENDER = ("Файл адресован другому получателю, с этого адреса принять его нельзя. "
                 "Верните файл с адреса, на который он был отправлен.")


class AcceptOutcome(str, Enum):
    APPLIED = "applied"
    ERRORS_SENT = "errors_sent"
    IGNORED_AUTOREPLY = "ignored_autoreply"
    REJECTED_SENDER = "rejected_sender"
    HINT_SENT = "hint_sent"


SYSTEM_SENDERS = {"mailer-daemon", "postmaster"}


def _is_autoreply(mail: IncomingMail, own_address: str | None = None) -> bool:
    """Автоответы, отчёты о недоставке и наши собственные письма: отвечать нельзя (петля)."""
    h = {k.lower(): v.strip().lower() for k, v in mail.headers.items()}
    if h.get("return-path") == "<>" or h.get("content-type", "").startswith("multipart/report"):
        return True
    addr = parseaddr(mail.sender)[1].strip().lower()
    if addr.rpartition("@")[0] in SYSTEM_SENDERS:
        return True
    if own_address and addr == parseaddr(own_address)[1].strip().lower():
        return True
    if h.get("auto-submitted", "no") != "no" or "x-autoreply" in h:
        return True
    if h.get("precedence") in AUTOREPLY_PRECEDENCE:
        return True
    return mail.subject.strip().lower().startswith(AUTOREPLY_SUBJECTS)


def _reply(mailer: Mailer, mail: IncomingMail, body: str) -> None:
    subject = mail.subject.strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    to = parseaddr(mail.sender)[1] or mail.sender
    mailer.send(to, subject, body, [], in_reply_to=mail.message_id)


def _numbered(lines: list[str]) -> str:
    items = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))
    return f"Файл не принят, ничего не изменено. Исправьте и верните ещё раз:\n{items}"


def _first_valid(paths: list[Path]) -> tuple[Path, int] | None:
    for path in paths:
        try:
            return path, read_meta(path)[0]
        except MetaError:
            continue
    return None


def accept_review_mail(session: Session, mail: IncomingMail, mailer: Mailer, *,
                       own_address: str | None = None) -> AcceptOutcome:
    """Разбирает ответ на файл уточнения и отвечает отправителю.

    own_address — адрес служебного ящика (env.service_mailbox): письма с него игнорируются.
    Ошибка mailer.send пробрасывается наружу. При APPLIED изменения уже в сессии (flush),
    поэтому вызывающий обязан откатить сессию и НЕ вызывать mark_done: письмо останется
    во входящих и будет разобрано заново.
    """
    mail_key = hashlib.sha256(mail.message_id.encode()).hexdigest()[:8]
    if _is_autoreply(mail, own_address):
        log.info("письмо %s: автоответ, пропущено", mail_key)
        return AcceptOutcome.IGNORED_AUTOREPLY
    xlsx = [p for p in mail.attachments if p.suffix.lower() == ".xlsx"]
    if not xlsx:
        _reply(mailer, mail, HINT_NO_XLSX)
        return AcceptOutcome.HINT_SENT
    found = _first_valid(xlsx)
    review = session.get(ReviewFile, found[1]) if found else None
    if found is None or review is None or review.kind not in ("executor", "owner"):
        _reply(mailer, mail, HINT_NOT_FROM_SYSTEM)
        return AcceptOutcome.HINT_SENT
    path = found[0]

    employee = session.get(Employee, review.employee_id) if review.employee_id else None
    expected = (employee.email or "").strip().lower() if employee else ""
    if not expected or parseaddr(mail.sender)[1].strip().lower() != expected:
        _reply(mailer, mail, REJECT_SENDER)
        log.info("письмо %s: отправитель не адресат файла %d", mail_key, review.id)
        return AcceptOutcome.REJECTED_SENDER

    is_owner = review.kind == "owner"
    parsed = parse_owner_file(path) if is_owner else parse_executor_file(path)
    if isinstance(parsed, ReviewErrors):
        _reply(mailer, mail, _numbered(parsed.lines))
        return AcceptOutcome.ERRORS_SENT
    try:
        report = (apply_owner_review if is_owner else apply_executor_review)(
            session, review, parsed)
    except ReviewRejected as rejected:
        _reply(mailer, mail, _numbered(rejected.errors.lines))
        return AcceptOutcome.ERRORS_SENT
    if is_owner:
        body = f"Принято: утверждено {report.approved}, отклонено {report.rejected}."
        if report.skipped:
            body += f" Строк, обработанных ранее: {report.skipped}."
        _reply(mailer, mail, body)
        log.info("письмо %s: файл владельца %d принят, утверждено %d, отклонено %d", mail_key,
                 review.id, report.approved, report.rejected)
        return AcceptOutcome.APPLIED
    body = f"Принято: подтверждено {report.confirmed}, передано владельцу реестра {report.to_owner}."
    if report.skipped:
        body += f" Строк, обработанных ранее: {report.skipped}."
    _reply(mailer, mail, body)
    log.info("письмо %s: файл %d принят, подтверждено %d, владельцу %d", mail_key, review.id,
             report.confirmed, report.to_owner)
    return AcceptOutcome.APPLIED
