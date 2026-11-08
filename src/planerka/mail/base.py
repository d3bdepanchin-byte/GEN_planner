"""Интерфейсы почты. Реализации: smtp.py, imap.py (настоящие), fake.py (тесты)."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


# Ответ системы всегда помечен как автоматический: два автоответчика не зацикливаются.
AUTO_HEADERS = {"Auto-Submitted": "auto-replied", "Precedence": "auto_reply"}


@dataclass
class IncomingMail:
    message_id: str
    sender: str
    subject: str
    headers: dict[str, str] = field(default_factory=dict)
    attachments: list[Path] = field(default_factory=list)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str, attachments: list[Path],
             in_reply_to: str | None = None) -> str:
        """Отправляет письмо со служебного ящика, возвращает Message-ID."""
        ...


class ServiceInbox(Protocol):
    done_folder: str  # куда перекладываются обработанные письма

    def fetch_new(self) -> list[IncomingMail]: ...

    def mark_done(self, message_id: str) -> None:
        """Письмо обработано: убрать из входящих (Д-75)."""
        ...
class MailError(RuntimeError):
    """Команда почтового сервера не выполнена; письмо осталось на месте."""
