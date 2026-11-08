from datetime import date
from pathlib import Path

import openpyxl
import pytest

from planerka.core.numbering import next_task_number
from planerka.core.statuses import TaskStatus
from planerka.db.models import Task
from planerka.mail.accept import AcceptOutcome, accept_review_mail
from planerka.mail.base import IncomingMail
from planerka.mail.fake import FakeInbox, FakeMailer
from planerka.review.executor import build_executor_file

ADDR = 'smirnov@example.test'


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def smirnov(employees):
    employees[0].email = ADDR
    return employees[0]


@pytest.fixture
def review_tasks(session, smirnov):
    tasks = []
    for title in ('Подготовить прайс-лист', 'Согласовать график поставок'):
        t = Task(number=next_task_number(session), title=title, type='поручение', priority='B',
                 owner_id=smirnov.id, due_date=date(2026, 11, 14), questions=[],
                 status=TaskStatus.CANDIDATE.value)
        session.add(t)
        tasks.append(t)
    session.flush()
    return tasks


@pytest.fixture
def review(session, smirnov, review_tasks, tmp_path):
    return build_executor_file(session, smirnov, review_tasks, tmp_path / 'out')


def fill(path, decision='подтверждаю'):
    wb = openpyxl.load_workbook(path)
    ws = wb['Задачи']
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, 1).value:
            ws.cell(r, 10).value = decision
    wb.save(path)
    return Path(path)


def make_mail(attachments=(), sender=ADDR, subject='Re: Уточнение задач', headers=None):
    return IncomingMail(message_id='<m1@example.test>', sender=sender, subject=subject,
                        headers=headers or {}, attachments=list(attachments))


@pytest.fixture
def autoreply_mail():
    return make_mail(headers={'Auto-Submitted': 'auto-replied'})


@pytest.fixture
def mail_no_attachment():
    return make_mail()


@pytest.fixture
def mail_with_pdf(tmp_path):
    pdf = tmp_path / 'scan.pdf'
    pdf.write_bytes(b'%PDF')
    return make_mail([pdf])


@pytest.fixture
def mail_valid(review):
    return make_mail([fill(review.path)])


@pytest.fixture
def mail_from_other(review):
    return make_mail([fill(review.path)], sender='Иван <ivan@example.test>')


def test_auto_reply_ignored(session, mailer, autoreply_mail):
    assert accept_review_mail(session, autoreply_mail, mailer) == AcceptOutcome.IGNORED_AUTOREPLY
    assert mailer.sent == []


@pytest.mark.parametrize('headers, subject', [
    ({'Auto-Submitted': 'auto-generated'}, 'Re: x'),
    ({'auto-submitted': 'auto-replied'}, 'Re: x'),
    ({'X-Autoreply': 'yes'}, 'Re: x'),
    ({'Precedence': 'bulk'}, 'Re: x'),
    ({'Precedence': 'Auto_Reply'}, 'Re: x'),
    ({'Precedence': 'junk'}, 'Re: x'),
    ({}, 'Автоответ: Уточнение'),
    ({}, 'Auto: Out of office'),
])
def test_autoreply_rules(session, mailer, headers, subject):
    mail = make_mail(headers=headers, subject=subject)
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.IGNORED_AUTOREPLY


def test_auto_submitted_no_is_not_autoreply(session, mailer):
    mail = make_mail(headers={'Auto-Submitted': 'no'})
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.HINT_SENT


def test_reply_without_attachment_gets_hint(session, mailer, mail_no_attachment):
    assert accept_review_mail(session, mail_no_attachment, mailer) == AcceptOutcome.HINT_SENT
    assert mailer.sent[-1]['to'] == mail_no_attachment.sender
    assert '.xlsx' in mailer.sent[-1]['body']


def test_reply_subject_and_threading(session, mailer, mail_no_attachment):
    accept_review_mail(session, mail_no_attachment, mailer)
    assert mailer.sent[-1]['subject'] == 'Re: Уточнение задач'
    assert mailer.sent[-1]['in_reply_to'] == mail_no_attachment.message_id


def test_forwarded_from_other_address_rejected(session, mailer, mail_from_other, review_tasks):
    assert accept_review_mail(session, mail_from_other, mailer) == AcceptOutcome.REJECTED_SENDER
    assert all(t.status == 'на подтверждении' for t in review_tasks)
    assert mailer.sent[-1]['to'] == 'ivan@example.test'


def test_non_xlsx_attachment_rejected(session, mailer, mail_with_pdf):
    assert accept_review_mail(session, mail_with_pdf, mailer) == AcceptOutcome.HINT_SENT


def test_valid_return_applied_and_confirmed(session, mailer, mail_valid, review_tasks):
    assert accept_review_mail(session, mail_valid, mailer) == AcceptOutcome.APPLIED
    assert all(t.status == 'подтверждена исполнителем' for t in review_tasks)
    assert mailer.sent[-1]['body'] == 'Принято: подтверждено 2, передано владельцу реестра 0.'


def test_sender_compared_case_insensitive_with_display_name(session, mailer, review, review_tasks):
    mail = make_mail([fill(review.path)], sender='Смирнов Александр <Smirnov@EXAMPLE.test>')
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.APPLIED
    assert mailer.sent[-1]['to'].lower() == ADDR


def test_foreign_xlsx_gets_not_from_system(session, mailer, tmp_path):
    path = tmp_path / 'other.xlsx'
    openpyxl.Workbook().save(path)
    assert accept_review_mail(session, make_mail([path]), mailer) == AcceptOutcome.HINT_SENT
    assert 'не из системы' in mailer.sent[-1]['body']


def test_first_valid_xlsx_used(session, mailer, review, review_tasks, tmp_path):
    junk = tmp_path / 'a.xlsx'
    openpyxl.Workbook().save(junk)
    mail = make_mail([junk, fill(review.path)])
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.APPLIED


def test_owner_file_treated_as_unknown(session, mailer, review):
    review.kind = 'owner'  # TODO Task 18: приём файла владельца реестра
    session.flush()
    mail = make_mail([fill(review.path)])
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.HINT_SENT
    assert 'не из системы' in mailer.sent[-1]['body']


def test_errors_sent_as_numbered_list(session, mailer, review, review_tasks):
    mail = make_mail([fill(review.path, decision='может быть')])
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.ERRORS_SENT
    body = mailer.sent[-1]['body']
    assert '1. строка 2' in body and '2. строка 3' in body
    assert all(t.status == 'на подтверждении' for t in review_tasks)


def test_rejected_by_apply_sent_as_errors(session, mailer, review, review_tasks):
    wb = openpyxl.load_workbook(review.path)
    ws = wb['Задачи']
    ws.cell(2, 10).value = 'правка'
    ws.cell(2, 13).value = 'Несуществующий Человек'
    ws.cell(3, 10).value = 'подтверждаю'
    wb.save(review.path)
    assert accept_review_mail(session, make_mail([Path(review.path)]), mailer) == \
        AcceptOutcome.ERRORS_SENT
    assert 'не найден в справочнике' in mailer.sent[-1]['body']
    assert all(t.status == 'на подтверждении' for t in review_tasks)


def test_fake_inbox_mark_done():
    m = make_mail()
    inbox = FakeInbox([m])
    assert inbox.fetch_new() == [m]
    inbox.mark_done(m.message_id)
    assert inbox.fetch_new() == []


@pytest.mark.parametrize('kwargs', [
    {'headers': {'Return-Path': '<>'}},
    {'sender': 'MAILER-DAEMON@example.test'},
    {'sender': 'Mail Delivery <Postmaster@example.test>'},
    {'headers': {'Content-Type': 'multipart/report; report-type=delivery-status'}},
    {'sender': 'Planerka <Service@example.test>'},
])
def test_loop_guards_no_reply(session, mailer, kwargs):
    mail = make_mail(**kwargs)
    out = accept_review_mail(session, mail, mailer, own_address='service@example.test')
    assert out == AcceptOutcome.IGNORED_AUTOREPLY
    assert mailer.sent == []


def test_return_path_normal_is_not_ignored(session, mailer):
    mail = make_mail(headers={'Return-Path': '<smirnov@example.test>'})
    assert accept_review_mail(session, mail, mailer) == AcceptOutcome.HINT_SENT


def test_replies_carry_auto_markers():
    """Контракт Mailer: ответ (с in_reply_to) помечается как автоматический."""
    from planerka.mail.fake import FakeMailer
    m = FakeMailer()
    m.send('a@example.test', 's', 'b', [], in_reply_to='<x@y>')
    assert m.sent[-1]['headers'] == {'Auto-Submitted': 'auto-replied', 'Precedence': 'auto_reply'}
    m.send('a@example.test', 's', 'b', [])
    assert m.sent[-1]['headers'] == {}


class BrokenMailer:
    def send(self, *a, **k):
        raise RuntimeError('smtp down')


def test_mailer_failure_propagates_and_rolls_back(session, review_tasks, mail_valid):
    with pytest.raises(RuntimeError):
        with session.begin_nested():  # вызывающий откатывает сессию при ошибке отправки
            accept_review_mail(session, mail_valid, BrokenMailer())
    for t in review_tasks:
        session.refresh(t)
    assert all(t.status == 'на подтверждении' for t in review_tasks)
