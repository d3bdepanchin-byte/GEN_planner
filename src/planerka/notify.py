"""Алерты разработчику и проверка текста на ПДн."""
from datetime import datetime, timezone

from planerka.core.directory import EmployeeRef
from planerka.mail.base import Mailer
from planerka.masking.names import WORD, lemma
from planerka.masking.patterns import find_contacts


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
