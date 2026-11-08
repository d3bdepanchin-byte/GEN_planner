"""Повтор вручную (`planerka requeue`): выход из состояний, где конвейер ждёт человека.

Что ставится в очередь, решает статус источника. Обработчики идемпотентны: лишний повтор
не создаёт дублей (send_reviews шлёт только неотправленное).
"""
from sqlalchemy.orm import Session

from planerka.db.models import Source
from planerka.extract.run import EXTRACTED
from planerka.pipeline.handlers.common import (
    KIND_PLANERKA, MASKING_UNCERTAIN, NOT_A_MEETING, RECEIVED, TRANSCRIBED,
)
from planerka.pipeline.jobs import EXTRACT, SEND_REVIEWS, TRANSCRIBE, enqueue


class RequeueRefused(Exception):
    """Повторять нечего или нельзя; сообщение — для человека, без ПДн."""


def requeue(session: Session, source_id: int, as_planerka: bool = False) -> str:
    """Ставит задание по статусу источника, возвращает его вид."""
    source = session.get(Source, source_id)
    if source is None:
        raise RequeueRefused(f'Источника {source_id} нет.')
    status = source.status
    if status == NOT_A_MEETING and not as_planerka:
        raise RequeueRefused(f'Источник {source_id}: «{status}». Если это планёрка — '
                             f'planerka requeue {source_id} --as-planerka.')
    if status == NOT_A_MEETING or status == MASKING_UNCERTAIN:
        if status == NOT_A_MEETING:
            source.kind = KIND_PLANERKA
        source.status = TRANSCRIBED
        kind = EXTRACT
    elif status == RECEIVED:
        kind = TRANSCRIBE
    elif status == TRANSCRIBED:
        kind = EXTRACT
    elif status == EXTRACTED:
        kind = SEND_REVIEWS
    else:
        raise RequeueRefused(f'Источник {source_id}: «{status}» — повторять нечего.')
    enqueue(session, kind, {'source_id': source.id})
    return kind
