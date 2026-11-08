"""Общее для обработчиков: статусы источника, алерт разработчику, даты в письмах."""
from datetime import date

from sqlalchemy.orm import Session

from planerka.core.directory import load_directory
from planerka.notify import send_alert_safe
from planerka.pipeline.deps import Deps
from planerka.settings import get_setting

# Статусы источника (Source.status) по ходу конвейера.
RECEIVED = 'получен'  # принят из приёмника (intake.scanner)
TRANSCRIBED = 'распознан'
NO_SPEECH = 'нет речи'
NOT_A_MEETING = 'не встреча'
DEFERRED_1B = 'отложено до 1б'  # Д-70
MASKING_UNCERTAIN = 'маскирование не уверено'

# Вид источника после фразы открытия.
KIND_PLANERKA = 'планёрка'
KIND_ONE_ON_ONE = 'встреча 1:1'


def alert(session: Session, deps: Deps, kind: str, source_id: int | None, detail: str) -> bool:
    """Алерт разработчику. detail — без ПДн: номера, счётчики, типы ошибок."""
    developer = get_setting(session, 'alerts.developer_email')
    return send_alert_safe(deps.mailer, [developer] if developer else [], kind, source_id,
                           detail, load_directory(session))


def ru_date(d: date | None) -> str:
    return d.strftime('%d.%m.%Y') if d is not None else 'без даты'
