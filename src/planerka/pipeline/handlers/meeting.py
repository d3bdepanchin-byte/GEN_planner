"""Запись встречи: приёмник → распознавание → фильтр по фразе открытия → разбор.

Обработчик идемпотентен: сначала смотрит статус источника. Письмо ведущему уходит после
сохранения статуса; не ушло — повтор задания отправит его без нового распознавания.
"""
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from planerka.asr.base import NoSpeech
from planerka.asr.filter import MeetingKind, detect_meeting_kind
from planerka.db.models import Employee, Source
from planerka.extract.run import ExtractDeps, extract_source
from planerka.intake.audio import extract_audio
from planerka.intake.scanner import INBOX_NAME_FILE
from planerka.masking.checker import MaskingUncertain
from planerka.pipeline.deps import Deps
from planerka.pipeline.handlers.common import (
    DEFERRED_1B, KIND_ONE_ON_ONE, KIND_PLANERKA, MASKING_UNCERTAIN, NO_SPEECH, NOT_A_MEETING,
    RECEIVED, TRANSCRIBED, alert, requeue_hint, ru_date,
)
from planerka.pipeline.jobs import EXTRACT, SEND_REVIEWS, TRANSCRIBE, enqueue
from planerka.pipeline.transcripts import load_transcript, meeting_dir, save_transcript
from planerka.settings import get_setting

log = logging.getLogger(__name__)

HOST_ASKED = 'ведущему написано'  # флаг источника: письмо ведущему ушло


def scan_inbox(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    sources = deps.intake().scan(session, now)
    for source in sources:
        enqueue(session, TRANSCRIBE, {'source_id': source.id}, now)
    if sources:
        log.info('приёмник: новых источников %d', len(sources))


def opening_phrases(session: Session) -> dict[str, list[str]]:
    return {k.value: list(get_setting(session, f'intake.opening_phrases.{k.value}'))
            for k in MeetingKind}


def transcribe(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    source = session.get(Source, payload['source_id'])
    if source is None:
        log.warning('распознавание: источника %s нет', payload['source_id'])
        return
    if source.status in (NO_SPEECH, NOT_A_MEETING) and not source.flags.get(HOST_ASKED):
        _ask_host(session, deps, source)  # прошлая попытка не дописала ведущему
        return
    if source.status != RECEIVED:
        log.info('распознавание: источник %s уже в статусе «%s»', source.id, source.status)
        return

    folder = meeting_dir(deps.env, source.id)
    wav = extract_audio(Path(source.path), folder)
    try:
        transcript = deps.transcriber.transcribe(wav)
        save_transcript(folder, transcript)
        kind = detect_meeting_kind(transcript, opening_phrases(session),
                                   int(get_setting(session, 'intake.opening_window_sec')))
    except NoSpeech:
        kind, source.status = None, NO_SPEECH
    finally:
        wav.unlink(missing_ok=True)  # звук восстанавливается из оригинала

    if kind is MeetingKind.PLANERKA:
        source.kind, source.status = KIND_PLANERKA, TRANSCRIBED
        enqueue(session, EXTRACT, {'source_id': source.id}, now)
    elif kind is MeetingKind.ONE_ON_ONE:
        source.kind, source.status = KIND_ONE_ON_ONE, DEFERRED_1B
    elif source.status != NO_SPEECH:
        source.status = NOT_A_MEETING
    log.info('источник %s распознан: %s', source.id, source.status)
    if source.status in (NO_SPEECH, NOT_A_MEETING):
        session.commit()  # статус сохранён до письма: повтор не распознаёт заново
        if source.status == NOT_A_MEETING:  # ведущий может переслать письмо: номер — здесь
            alert(session, deps, NOT_A_MEETING, source.id,
                  f'нет фразы открытия, запись от {ru_date(source.meeting_date)}; если ведущий '
                  f'подтвердит планёрку — {requeue_hint(source.id, as_planerka=True)}')
        _ask_host(session, deps, source)


NO_REPLY = 'На это письмо не отвечайте: ящик читает только система.'


def _recording(deps: Deps, source: Source, when: str) -> str:
    """«записи «имя файла» от дата» — имя из папки встречи на сервере, если есть."""
    path = meeting_dir(deps.env, source.id) / INBOX_NAME_FILE
    name = path.read_text(encoding='utf-8').strip() if path.is_file() else ''
    return f'записи «{name}» от {when}' if name else f'записи от {when}'


def _host_message(session: Session, deps: Deps, source: Source) -> tuple[str, str]:
    when = ru_date(source.meeting_date)
    recording = _recording(deps, source, when)
    developer = get_setting(session, 'alerts.developer_email')
    forward = [f'Если это планёрка — перешлите это письмо на {developer}.'] if developer else []
    if source.status == NO_SPEECH:
        lines = [f'В {recording} не распознана речь, задачи не созданы.',
                 'Если файл не тот — положите в папку правильную запись.', *forward]
        return f'Запись {when}: нет речи', '\n'.join([*lines, NO_REPLY]) + '\n'
    phrases = list(get_setting(session, 'intake.opening_phrases.planerka'))
    phrase = phrases[0][:1].upper() + phrases[0][1:] if phrases else ''
    lines = [f'В начале {recording} нет фразы открытия: запись не разобрана, задачи не созданы.',
             *forward,
             f'Чтобы запись разобралась автоматически, планёрку начинают с фразы «{phrase}».']
    return f'Запись {when}: нет фразы открытия', '\n'.join([*lines, NO_REPLY]) + '\n'


def _ask_host(session: Session, deps: Deps, source: Source) -> None:
    host_id = int(get_setting(session, 'meeting.host_employee_id'))
    host = session.get(Employee, host_id) if host_id else None
    if host is None or not host.email:
        alert(session, deps, 'нет ведущего', source.id,
              f'источник «{source.status}», не задан ведущий или его e-mail')
    else:
        subject, body = _host_message(session, deps, source)
        deps.mailer.send(host.email, subject, body, [])
    source.flags = {**source.flags, HOST_ASKED: True}


def extract(session: Session, deps: Deps, payload: dict, now: datetime) -> None:
    source = session.get(Source, payload['source_id'])
    if source is None or source.kind != KIND_PLANERKA or source.status != TRANSCRIBED:
        log.info('разбор: источник %s пропущен (%s)', payload['source_id'],
                 'нет' if source is None else source.status)
        return
    transcript = load_transcript(meeting_dir(deps.env, source.id))
    xdeps = ExtractDeps(masker=deps.masker, names=deps.names, directory=deps.masker.directory,
                        tokens=deps.tokens, llm=deps.llm, prompt=deps.prompt)
    try:
        with session.begin_nested():
            extract_source(session, source, transcript, xdeps)
    except MaskingUncertain as e:  # в LLM ничего не ушло; повтор не поможет — решает человек
        source.status = MASKING_UNCERTAIN
        log.warning('источник %s: маскирование не уверено, остатков %d', source.id, e.count)
        alert(session, deps, MASKING_UNCERTAIN, source.id,
              f'остатков {e.count}; после правки справочника или стоп-листа — '
              f'{requeue_hint(source.id)}')
        return
    enqueue(session, SEND_REVIEWS, {'source_id': source.id}, now)
