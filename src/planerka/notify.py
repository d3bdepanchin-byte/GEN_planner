"""Алерты разработчику и проверка текста на ПДн."""
import logging
from datetime import datetime, timezone

from planerka.core.directory import EmployeeRef
from planerka.mail.base import Mailer
from planerka.masking.names import WORD, lemma
from planerka.masking.patterns import find_contacts

log = logging.getLogger(__name__)
HIDDEN_DETAIL = "текст скрыт: содержал ПДн"


def assert_no_pii(text: str, directory: list[EmployeeRef]) -> None:
    """ValueError, если в тексте есть имя из справочника (по лемме слова) или контакт.

    Сообщение об ошибке не содержит найденного текста: оно попадает в логи.
    """
    known = {lemma(w) for e in directory
             for variant in (e.full_name, *e.variants) for w in WORD.findall(variant)}
    if any(lemma(w) in known for w in WORD.findall(text)):
        raise ValueError("в тексте алерта есть имя сотрудника")
    if find_contacts(text):
        raise ValueError("в тексте алерта есть контакт")


def send_alert(mailer: Mailer, to: list[str], kind: str, source_id: int | None,
               detail: str, directory: list[EmployeeRef]) -> None:
    assert_no_pii(detail, directory)
    if not to:
        return
    source = "—" if source_id is None else str(source_id)
    at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"Тип: {kind}\nИсточник: {source}\nОписание: {detail}\nВремя: {at}\n"
    for address in to:
        mailer.send(address, f"Планёрка: алерт {kind}", body, [])


def send_alert_safe(mailer: Mailer, to: list[str], kind: str, source_id: int | None,
                    detail: str, directory: list[EmployeeRef]) -> bool:
    """Алерт, который не роняет вызывающего: ПДн в detail скрываются, ошибки почты — в лог."""
    try:
        try:
            send_alert(mailer, to, kind, source_id, detail, directory)
        except ValueError:
            send_alert(mailer, to, kind, source_id, HIDDEN_DETAIL, directory)
    except Exception as e:  # noqa: BLE001 — алерт не должен ронять работу; тип без текста (ПДн)
        log.error("алерт %s не отправлен: %s", kind, type(e).__name__)
        return False
    return True
