from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from planerka.core.numbering import format_number
from planerka.core.statuses import TaskStatus
from planerka.core.workdays import Calendar
from planerka.db.models import Employee, ReviewFile
from planerka.mail.fake import FakeMailer
from planerka.reminders import (
    ReminderAction, ReminderKind, due_actions, run_reminders, set_due,
)
from planerka.settings import set_setting

UTC = timezone.utc
FLAG = 'на ближайшую планёрку'


@pytest.fixture
def cal(tmp_path):
    (tmp_path / '2026.toml').write_text('holidays = [2026-11-04]')
    return Calendar.load(tmp_path)


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def owner(session):
    o = Employee(full_name='Орлова Нина', email='owner@example.test')
    session.add(o)
    session.flush()
    set_setting(session, 'registry.owner_employee_id', o.id, 'test')
    set_setting(session, 'alerts.developer_email', 'dev@example.test', 'test')
    return o


@pytest.fixture
def executor(employees):
    employees[0].email = 'smirnov@example.test'
    return employees[0]


@pytest.fixture
def sent_review(session, cal, executor, task, tmp_path):
    f = tmp_path / 'review.xlsx'
    f.write_bytes(b'x')
    rf = ReviewFile(kind='executor', employee_id=executor.id, path=str(f),
                    task_numbers=[task.number], sent_at=datetime(2026, 11, 3, 9, tzinfo=UTC))
    set_due(rf, cal, 2)
    session.add(rf)
    session.flush()
    return rf


def test_due_counts_workdays_skipping_holiday(cal):
    rf = ReviewFile(sent_at=datetime(2026, 11, 3, 9))
    set_due(rf, cal, 2)
    assert rf.due_at == datetime(2026, 11, 6, 9)


def test_not_due_before_deadline(session, cal, sent_review):
    assert due_actions(session, cal, datetime(2026, 11, 6, 8, 59, tzinfo=UTC)) == []


def test_executor_reminded_once_at_due(session, cal, sent_review):
    acts = due_actions(session, cal, sent_review.due_at)
    assert acts == [ReminderAction(ReminderKind.REMIND_EXECUTOR, sent_review.id)]
    sent_review.reminded_at = sent_review.due_at
    assert due_actions(session, cal, sent_review.due_at) == []


def test_owner_escalated_after_extra_workday(session, cal, sent_review):
    sent_review.reminded_at = sent_review.due_at
    before = datetime(2026, 11, 9, 8, tzinfo=UTC)
    assert due_actions(session, cal, before) == []
    later = cal.add_workdays(sent_review.due_at, 1)  # 6 ноября пт -> 9 ноября пн
    assert [a.kind for a in due_actions(session, cal, later)] == [ReminderKind.ESCALATE_OWNER]


def test_escalation_without_reminder_returns_both_reminder_first(session, cal, sent_review):
    later = cal.add_workdays(sent_review.due_at, 1)
    assert [a.kind for a in due_actions(session, cal, later)] == [
        ReminderKind.REMIND_EXECUTOR, ReminderKind.ESCALATE_OWNER]


def test_returned_file_stops_reminders(session, cal, sent_review):
    sent_review.returned_at = sent_review.due_at
    assert due_actions(session, cal, sent_review.due_at) == []


def test_unsent_and_owner_files_ignored(session, cal, executor):
    session.add(ReviewFile(kind='executor', employee_id=executor.id, path='x'))
    session.add(ReviewFile(kind='owner', employee_id=executor.id, path='y',
                           sent_at=datetime(2026, 11, 3, 9, tzinfo=UTC),
                           due_at=datetime(2026, 11, 5, 9, tzinfo=UTC)))
    session.flush()
    assert due_actions(session, cal, datetime(2026, 12, 1, tzinfo=UTC)) == []


def test_run_reminds_executor_with_file_and_is_idempotent(session, cal, mailer, owner, sent_review, task):
    now = sent_review.due_at
    assert run_reminders(session, cal, mailer, now) == 1
    m = mailer.sent[0]
    assert m['to'] == 'smirnov@example.test'
    assert m['subject'] == 'Файл на уточнение: напоминание'
    assert format_number(task.number) in m['body']
    assert [str(p) for p in m['attachments']] == [sent_review.path]
    assert sent_review.reminded_at == now
    assert run_reminders(session, cal, mailer, now) == 0
    assert len(mailer.sent) == 1


def test_remind_without_file_on_disk_sends_without_attachment(session, cal, mailer, owner, sent_review):
    sent_review.path = '/nonexistent/review.xlsx'
    assert run_reminders(session, cal, mailer, sent_review.due_at) == 1
    assert mailer.sent[0]['attachments'] == []


def test_run_escalates_flags_tasks_and_mails_owner(session, cal, mailer, owner, executor,
                                                    sent_review, task):
    other = task.__class__(number=task.number + 1, title='Другая', status=TaskStatus.IN_PROGRESS.value)
    session.add(other)
    session.flush()
    sent_review.task_numbers = [task.number, task.number, other.number]
    now = cal.add_workdays(sent_review.due_at, 1)
    assert run_reminders(session, cal, mailer, now) == 2
    assert [m['to'] for m in mailer.sent] == ['smirnov@example.test', 'owner@example.test']
    body = mailer.sent[1]['body']
    assert body.count(format_number(task.number)) == 1
    assert executor.full_name in body
    assert task.flags == [FLAG]
    assert other.flags == []
    assert sent_review.escalated_at == now
    assert run_reminders(session, cal, mailer, now) == 0


def test_missing_executor_email_alerts_and_keeps_timestamp(session, cal, mailer, owner, executor, sent_review):
    executor.email = None
    assert run_reminders(session, cal, mailer, sent_review.due_at) == 0
    assert sent_review.reminded_at is None
    assert [m['to'] for m in mailer.sent] == ['dev@example.test']
    body = mailer.sent[0]['body']
    assert 'напоминание' in body and executor.full_name not in body


def test_missing_owner_email_alerts_and_keeps_timestamp(session, cal, mailer, owner, sent_review, task):
    owner.email = None
    sent_review.reminded_at = sent_review.due_at
    now = cal.add_workdays(sent_review.due_at, 1)
    assert run_reminders(session, cal, mailer, now) == 0
    assert sent_review.escalated_at is None
    assert task.flags == []
    assert [m['to'] for m in mailer.sent] == ['dev@example.test']
