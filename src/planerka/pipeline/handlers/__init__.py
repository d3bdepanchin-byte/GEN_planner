"""Обработчики заданий: вид задания → функция (session, deps, payload, now)."""
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from planerka.pipeline import jobs
from planerka.pipeline.deps import Deps
from planerka.pipeline.handlers import meeting, review

Handler = Callable[[Session, Deps, dict, datetime], None]

HANDLERS: dict[str, Handler] = {
    jobs.SCAN_INBOX: meeting.scan_inbox,
    jobs.TRANSCRIBE: meeting.transcribe,
    jobs.EXTRACT: meeting.extract,
    jobs.SEND_REVIEWS: review.send_reviews,
    jobs.POLL_MAIL: review.poll_mail,
    jobs.REMINDERS: review.reminders,
    jobs.OWNER_DIGEST: review.owner_digest,
}
