from pathlib import Path

from planerka.mail.base import AUTO_HEADERS, IncomingMail


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to: str, subject: str, body: str, attachments: list[Path],
             in_reply_to: str | None = None) -> str:
        message_id = f"<fake-{len(self.sent) + 1}@example.test>"
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "attachments": list(attachments), "in_reply_to": in_reply_to,
                          "headers": dict(AUTO_HEADERS) if in_reply_to else {},
                          "message_id": message_id})
        return message_id


class FakeInbox:
    def __init__(self, messages: list[IncomingMail] | None = None) -> None:
        self.messages = list(messages or [])
        self.done: list[str] = []

    def fetch_new(self) -> list[IncomingMail]:
        return [m for m in self.messages if m.message_id not in self.done]

    def mark_done(self, message_id: str) -> None:
        self.done.append(message_id)
