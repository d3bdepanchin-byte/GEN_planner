import pytest
from sqlalchemy import select

from planerka.db.models import Job, ReviewFile, Source
from planerka.pipeline.requeue import RequeueRefused, requeue
from samples import DEV, EXPECTED_CANDIDATES, PLANERKA, TWO_CANDIDATES, tasks_of


def _queued(session, source):
    return [(j.kind, j.payload) for j in session.scalars(
        select(Job).where(Job.status == 'pending').order_by(Job.id))
        if j.payload.get('source_id') == source.id]


@pytest.mark.parametrize('status, kind', [('получен', 'transcribe'), ('распознан', 'extract'),
                                          ('разобран', 'send_reviews')])
def test_requeue_by_status(session, source_ready, status, kind):
    session.execute(Job.__table__.delete())
    source_ready.status = status
    assert requeue(session, source_ready.id) == kind
    assert _queued(session, source_ready) == [(kind, {'source_id': source_ready.id})]
    assert session.get(Source, source_ready.id).status == status


def test_requeue_after_masking_uncertain(session, deps_leaky_text, make_deps, source_ready,
                                        run_until_idle):
    run_until_idle(session, deps_leaky_text)
    assert source_ready.status == 'маскирование не уверено'
    assert requeue(session, source_ready.id) == 'extract'
    assert source_ready.status == 'распознан'
    run_until_idle(session, make_deps(PLANERKA, TWO_CANDIDATES))  # справочник поправили
    assert len(tasks_of(session, source_ready)) == EXPECTED_CANDIDATES


def test_requeue_not_a_meeting_as_planerka(session, deps_no_phrase, make_deps, source_ready,
                                           run_until_idle):
    run_until_idle(session, deps_no_phrase)
    with pytest.raises(RequeueRefused, match='--as-planerka'):
        requeue(session, source_ready.id)
    assert requeue(session, source_ready.id, as_planerka=True) == 'extract'
    assert (source_ready.kind, source_ready.status) == ('планёрка', 'распознан')
    run_until_idle(session, make_deps(PLANERKA, TWO_CANDIDATES))
    assert len(tasks_of(session, source_ready)) == EXPECTED_CANDIDATES


def test_requeue_sends_file_after_email_added(session, make_deps, source_ready, executor,
                                              run_until_idle):
    executor.email = None
    deps = make_deps(PLANERKA, TWO_CANDIDATES)
    run_until_idle(session, deps)
    alert = [m for m in deps.mailer.sent if m['to'] == DEV][-1]
    assert f'planerka requeue {source_ready.id}' in alert['body']
    assert session.scalars(select(ReviewFile).where(ReviewFile.kind == 'executor')).all() == []

    executor.email = 'smirnov@example.test'
    assert requeue(session, source_ready.id) == 'send_reviews'
    run_until_idle(session, deps)
    assert [m['to'] for m in deps.mailer.sent].count('smirnov@example.test') == 1


@pytest.mark.parametrize('status', ['отложено до 1б', 'нет речи', 'заменён'])
def test_requeue_refuses_other_statuses(session, source_ready, status):
    source_ready.status = status
    with pytest.raises(RequeueRefused):
        requeue(session, source_ready.id)


def test_requeue_unknown_source(session):
    with pytest.raises(RequeueRefused):
        requeue(session, 10**9)


def test_requeue_source_waiting_after_replacing_extracted(session, source_ready):
    session.execute(Job.__table__.delete())
    source_ready.status = 'ждёт решения: заменяет разобранную запись'
    assert requeue(session, source_ready.id) == 'transcribe'
    assert _queued(session, source_ready) == [('transcribe', {'source_id': source_ready.id})]
    assert session.get(Source, source_ready.id).status == 'получен'
