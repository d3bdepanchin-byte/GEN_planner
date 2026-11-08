"""Замена ПДн токенами. Порядок: контакты, сотрудники, остальные имена (NER), ИП.

Названия компаний, цены, обороты не маскируются (правило 5, Д-21). Токены вставляются
как есть, без склонения: «со [P1]». Логов и текста в исключениях здесь нет.
"""
import re
from dataclasses import dataclass, field

from planerka.core.directory import EmployeeRef
from planerka.masking.names import NameFinder, match_employees
from planerka.masking.patterns import Span, find_contacts
from planerka.masking.tokens import TokenStore, token_number

# Название компании с организационно-правовой формой в кавычках: имена внутри — часть названия.
_COMPANY = re.compile(r'(?<![^\W\d_])(?:ООО|ОАО|ЗАО|ПАО|АО|НАО|ТОО)\s*[«"„“][^»"“”\n]*[»"“”]')

_NAME = r'[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?'
_INITIALS = r'[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?'
# «ИП Волков», «ИП Волков И. П.», «ИП Волков Игорь Петрович», «ИП И. П. Волков».
# Распознавание может выдать текст без заглавных: «ип волков» — фамилия в любом регистре.
_IP = re.compile(
    r'(?<![^\W\d_])(?i:ИП|индивидуальн(?:ый|ого|ому|ым|ом) предпринимател[ьяюем]{0,2})\s+[«"]?'
    rf'((?:{_INITIALS}\s*)?(?P<surname>[А-ЯЁа-яё][а-яё]+(?:-[А-ЯЁа-яё][а-яё]+)?)'
    rf'(?:\s+{_INITIALS}|\s+{_NAME}(?:\s+{_NAME})?)?)'
)
# Служебные слова после «ип» — не фамилия: «ип и ооо».
_NOT_SURNAME = frozenset('и или а но по на в во с со от до для у к о об не это тоже же как'.split())


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


def _uncovered(span: Span, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Части спана вне уже занятых участков."""
    parts = [(span.start, span.end)]
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
                 threshold: int) -> None:
        self.directory = directory
        self.tokens = tokens
        self.names = names
        self.threshold = threshold

    def mask(self, text: str, source_id: int) -> MaskResult:
        result = MaskResult(text)
        repl: list[_Repl] = []

        for s in find_contacts(text):
            repl.append(_Repl(s.start, s.end, self.tokens.contact(source_id, s.kind, s.text)))

        for m in match_employees(text, self.directory, self.threshold):
            if _overlaps(m.span.start, m.span.end, repl):
                continue
            repl.append(_Repl(m.span.start, m.span.end, self._employee_token(m.employee_ids,
                                                                             result)))

        covered = [(r.start, r.end) for r in repl]
        companies = [(m.start(), m.end()) for m in _COMPANY.finditer(text)]
        parts = [u for s in self.names.find(text) for u in _uncovered(s, covered + companies)]
        parts += [u for m in _IP.finditer(text) if m['surname'].casefold() not in _NOT_SURNAME
                  for u in _uncovered(Span(m.start(1), m.end(1), 'IP', m.group(1)), covered)]
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
