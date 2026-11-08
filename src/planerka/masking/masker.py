"""Замена ПДн токенами. Порядок: контакты, сотрудники, остальные имена (NER и словарь), ИП.

Названия компаний, цены, обороты не маскируются (правило 5, Д-21), как и слова из стоп-листа
config/masking_stoplist.toml (бренды) — кроме имён сотрудников. Токены вставляются
как есть, без склонения: «со [P1]». Логов и текста в исключениях здесь нет.
"""
import re
from dataclasses import dataclass, field

from planerka.core.directory import EmployeeRef
from planerka.masking.names import (WORD, NameFinder, SurnameEdits, load_stoplist,
                                    match_employees, person_like, stop_spans)
from planerka.masking.patterns import find_contacts
from planerka.masking.tokens import TokenStore, token_number

# Название компании с организационно-правовой формой в кавычках: имена внутри — часть названия.
_COMPANY = re.compile(r'(?<![^\W\d_])(?:ООО|ОАО|ЗАО|ПАО|АО|НАО|ТОО)\s*[«"„“][^»"“”\n]*[»"“”]')

# «ИП Волков», «ИП Волков И. П.», «ИП А.С. Волков», «ип волкова анна сергеевна».
_IP_PREFIX = re.compile(
    r'(?<![^\W\d_])(?i:ИП|индивидуальн(?:ый|ого|ому|ым|ом)\s+предпринимател[ьяюем]{0,2})'
    r'\s+[«"]?')
_IP_PART = re.compile(
    r'\s*(?:(?P<init>[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?)|(?P<word>[^\W\d_]+(?:-[^\W\d_]+)*))')
_TITLE = re.compile(r'[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?')
_IP_MAX_WORDS = 3  # фамилия, имя, отчество
# Служебные слова после «ип» — не фамилия: «ип и ооо».
_NOT_SURNAME = frozenset('и или а но по на в во с со от до для у к о об не это тоже же как'.split())


def company_spans(text: str) -> list[tuple[int, int]]:
    """Названия компаний с организационно-правовой формой: не ПДн (Д-21)."""
    return [(m.start(), m.end()) for m in _COMPANY.finditer(text)]


def _ip_names(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Имена ИП и сами слова «ИП» (их не маскируем).

    Первое слово после «ИП» — фамилия в любом регистре (распознавание может не ставить
    заглавные); следующие — инициалы, слова с заглавной или имена, фамилии, отчества
    по словарю.
    """
    names, prefixes = [], []
    for m in _IP_PREFIX.finditer(text):
        prefixes.append((m.start(), m.end()))
        pos, start, words = m.end(), None, 0
        while (p := _IP_PART.match(text, pos)) is not None:
            word = p['word']
            if word is not None:
                ok = (word.casefold() not in _NOT_SURNAME if words == 0
                      else _TITLE.fullmatch(word) is not None or person_like(word))
                if not ok or words == _IP_MAX_WORDS:
                    break
                words += 1
            if start is None:
                start = p.start('word') if word is not None else p.start('init')
            pos = p.end()
        if words:
            names.append((start, pos))
    return names, prefixes


@dataclass
class MaskResult:
    text: str
    tokens: set[str] = field(default_factory=set)
    ambiguous: list[list[int]] = field(default_factory=list)


@dataclass(frozen=True)
class _Repl:
    start: int
    end: int
    token: str


def _overlaps(start: int, end: int, taken: list[_Repl]) -> bool:
    return any(start < r.end and r.start < end for r in taken)


def _uncovered(start: int, end: int,
               covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Части участка вне уже занятых."""
    parts = [(start, end)]
    for c_start, c_end in covered:
        nxt = []
        for s, e in parts:
            if c_end <= s or e <= c_start:
                nxt.append((s, e))
                continue
            if s < c_start:
                nxt.append((s, c_start))
            if c_end < e:
                nxt.append((c_end, e))
        parts = nxt
    return parts


_EDGE = ' \t\n,;:!?«»"„“”()'


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Обрезать пробелы и знаки по краям (точку инициалов оставить); без букв — None."""
    while start < end and text[start] in _EDGE + '.':
        start += 1
    while end > start and text[end - 1] in _EDGE:
        end -= 1
    return (start, end) if any(ch.isalpha() for ch in text[start:end]) else None


def _merge(text: str, parts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Слить пересекающиеся и разделённые только пробелами части: это один человек."""
    trimmed = sorted(p for u in parts if (p := _trim(text, *u)) is not None)
    merged: list[tuple[int, int]] = []
    for start, end in trimmed:
        if merged and (merged[-1][1] >= start or text[merged[-1][1]:start].isspace()):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class Masker:
    def __init__(self, directory: list[EmployeeRef], tokens: TokenStore, names: NameFinder,
                 edits: SurnameEdits) -> None:
        self.directory = directory
        self.tokens = tokens
        self.names = names
        self.edits = edits

    def mask(self, text: str, source_id: int) -> MaskResult:
        result = MaskResult(text)
        repl: list[_Repl] = []

        for s in find_contacts(text):
            repl.append(_Repl(s.start, s.end, self.tokens.contact(source_id, s.kind, s.text)))

        for m in match_employees(text, self.directory, self.edits):
            if _overlaps(m.span.start, m.span.end, repl):
                continue
            repl.append(_Repl(m.span.start, m.span.end, self._employee_token(m.employee_ids,
                                                                             result)))

        covered = [(r.start, r.end) for r in repl]
        ip_names, ip_prefixes = _ip_names(text)
        # Сотрудники уже в covered: стоп-лист их не скрывает.
        keep = covered + company_spans(text) + ip_prefixes + stop_spans(text, load_stoplist())
        parts = [u for s in self.names.find(text) for u in _uncovered(s.start, s.end, keep)]
        # Запасной проход после NER: natasha не видит имён в тексте без заглавных.
        parts += [u for w in WORD.finditer(text) if person_like(w.group())
                  for u in _uncovered(w.start(), w.end(), keep)]
        parts += [u for s, e in ip_names for u in _uncovered(s, e, covered + ip_prefixes)]
        for start, end in _merge(text, parts):
            repl.append(_Repl(start, end, self.tokens.external(source_id, text[start:end])))

        out, pos = [], 0
        for r in sorted(repl, key=lambda r: r.start):
            out += [text[pos:r.start], r.token]
            pos = r.end
            result.tokens.add(r.token)
        out.append(text[pos:])
        result.text = ''.join(out)
        return result

    def _employee_token(self, employee_ids: list[int], result: MaskResult) -> str:
        if len(employee_ids) == 1:
            return self.tokens.employee(employee_ids[0])
        pairs = sorted((token_number(self.tokens.employee(e)), e) for e in employee_ids)
        ids = [e for _, e in pairs]
        if ids not in result.ambiguous:
            result.ambiguous.append(ids)
        return '[' + '|'.join(f'P{n}' for n, _ in pairs) + ']'
