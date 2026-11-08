"""Вторая проверка замаскированного текста перед внешним LLM (правило 5, Д-19).

Независимый проход по выходу маскировщика: NER, точные леммы вариантов имён из справочника,
контакты, слова с граммемами имени, имена латиницей. Токены пропускаются. Бренды из стоп-листа
и короткие названия компаний в кавычках — не остаток, пока не входят в имя и не стоят рядом
с ним или с токеном человека. Есть остаток — текст не уходит.
Тексты найденных спанов в логи и исключения не попадают.
"""
from planerka.core.directory import EmployeeRef
from planerka.masking.masker import company_spans
from planerka.masking.names import (WORD, NameFinder, Range, SurnameEdits, latin_names,
                                    load_stoplist, match_employees, only_stop_words,
                                    person_like, stop_neighbours, stop_spans)
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


def _inside(start: int, end: int, ranges: list[Range]) -> bool:
    return any(start < r_end and r_start < end for r_start, r_end in ranges)


def _crosses(start: int, end: int, ranges: list[Range]) -> bool:
    """Спан задевает название компании, но не лежит в нём целиком."""
    return any(start < r_end and r_start < end and not (r_start <= start and end <= r_end)
               for r_start, r_end in ranges)


def check_residual(masked: str, directory: list[EmployeeRef], names: NameFinder) -> list[Span]:
    text = _blank_tokens(masked)
    found = [m.span for m in match_employees(text, directory, _EXACT)]
    found += find_contacts(text)
    companies = company_spans(text)
    stoplist = load_stoplist()
    stops = stop_spans(text, stoplist)
    people: list[Range] = []
    for s in names.find(text):
        if only_stop_words(text, s.start, s.end, stops):
            continue
        # Имя, вылезающее за кавычку названия, — не часть названия.
        crossing = _crosses(s.start, s.end, companies)
        people += [(w.start(), w.end()) for w in WORD.finditer(text, s.start, s.end)
                   if crossing or not _inside(w.start(), w.end(), companies)]
    people += [(w.start(), w.end()) for w in WORD.finditer(text)
               if person_like(w.group()) and (w.start(), w.end()) not in stops
               and not _inside(w.start(), w.end(), companies)]
    people += [r for r in latin_names(text, stoplist) if not _inside(*r, companies)]
    person_tokens = [(m.start(), m.end()) for m in TOKEN_IN_TEXT.finditer(masked)
                     if m.group()[1] in 'PX']
    people += stop_neighbours(text, stops, people + person_tokens)
    found += [Span(start, end, 'PER', text[start:end]) for start, end in people]
    out: list[Span] = []
    for s in sorted(found, key=lambda s: (s.start, -s.end)):
        if not out or s.start >= out[-1].end:  # пересечения считаем одним остатком
            out.append(s)
    return out


def ensure_clean(masked: str, directory: list[EmployeeRef], names: NameFinder) -> None:
    residual = check_residual(masked, directory, names)
    if residual:
        raise MaskingUncertain(len(residual))
