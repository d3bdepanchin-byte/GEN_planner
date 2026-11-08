"""Вторая проверка замаскированного текста перед внешним LLM (правило 5, Д-19).

Независимый проход по выходу маскировщика: NER, точные леммы вариантов имён из справочника,
контакты, слова с граммемами имени. Токены пропускаются. Есть остаток — текст не уходит.
Тексты найденных спанов в логи и исключения не попадают.
"""
from planerka.core.directory import EmployeeRef
from planerka.masking.masker import company_spans
from planerka.masking.names import (WORD, NameFinder, SurnameEdits, load_stoplist,
                                    match_employees, person_like, stop_spans)
from planerka.masking.patterns import Span, find_contacts
from planerka.masking.tokens import TOKEN_IN_TEXT

# Только точное совпадение леммы: нечёткое сравнение — работа маскировщика.
_EXACT = SurnameEdits(max_short=0, max_long=0, short_from=0, long_from=0)


class MaskingUncertain(Exception):
    """Во внешний LLM текст не отправляется; в сообщении только число остатков."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f'маскирование не уверено: остатков {count}')


def _blank_tokens(text: str) -> str:
    """Токены заменить пробелами той же длины: позиции спанов совпадают с исходным текстом."""
    return TOKEN_IN_TEXT.sub(lambda m: ' ' * len(m.group()), text)


def _inside(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < r_end and r_start < end for r_start, r_end in ranges)


def check_residual(masked: str, directory: list[EmployeeRef], names: NameFinder) -> list[Span]:
    text = _blank_tokens(masked)
    found = [m.span for m in match_employees(text, directory, _EXACT)]
    found += find_contacts(text)
    # Компании и стоп-лист не ПДн; сотрудники из справочника уже найдены выше.
    shielded = company_spans(text) + stop_spans(text, load_stoplist())
    words = [w for s in names.find(text) for w in WORD.finditer(text, s.start, s.end)]
    words += [w for w in WORD.finditer(text) if person_like(w.group())]
    found += [Span(w.start(), w.end(), 'PER', w.group()) for w in words
              if not _inside(w.start(), w.end(), shielded)]
    out: list[Span] = []
    for s in sorted(found, key=lambda s: (s.start, -s.end)):
        if not out or s.start >= out[-1].end:  # пересечения считаем одним остатком
            out.append(s)
    return out


def ensure_clean(masked: str, directory: list[EmployeeRef], names: NameFinder) -> None:
    residual = check_residual(masked, directory, names)
    if residual:
        raise MaskingUncertain(len(residual))
