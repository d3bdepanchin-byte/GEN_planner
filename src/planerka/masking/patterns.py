"""Контакты регулярными выражениями (Д-16): телефоны, e-mail, ИНН.

Цены и обороты не трогаем (Д-21): голое 10- или 12-значное число — ИНН только после
слова «ИНН», телефон без +7/8 — только в группировке 3-3-2-2 с разделителями.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str
    text: str


_SEP = r'[ \t\-]*'
_PHONE = re.compile(
    r'(?<![\d+])'
    r'(?:'
    rf'(?:\+7|8){_SEP}\(?\d{{3}}\)?{_SEP}\d{{3}}{_SEP}\d{{2}}{_SEP}\d{{2}}'
    r'|'
    r'(?<!\d[ \-])\(?\d{3}\)?[ \-]\d{3}[ \-]\d{2}[ \-]\d{2}(?![ \-]?\d)'
    r')'
    r'(?!\d)'
)
_EMAIL = re.compile(r'[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[^\W\d_]{2,}')
_INN = re.compile(
    r'(?<![^\W\d_])ИНН[\s:№\-]*'
    r'(\d{12}(?!\d)|\d{10}(?!\d)|(?:\d[ \-]?){11}\d(?!\d)|(?:\d[ \-]?){9}\d(?!\d))',
    re.IGNORECASE,
)


def find_contacts(text: str) -> list[Span]:
    """Спаны контактов по порядку в тексте; у ИНН спан — только цифры, без слова «ИНН»."""
    found: list[Span] = []
    for m in _EMAIL.finditer(text):
        found.append(Span(m.start(), m.end(), 'EMAIL', m.group()))
    for m in _INN.finditer(text):
        found.append(Span(m.start(1), m.end(1), 'INN', m.group(1)))
    for m in _PHONE.finditer(text):
        found.append(Span(m.start(), m.end(), 'TEL', m.group()))
    found.sort(key=lambda s: (s.start, -s.end))
    out: list[Span] = []
    for s in found:  # первый по позиции выигрывает при пересечении
        if not out or s.start >= out[-1].end:
            out.append(s)
    return out


def normalize_contact(kind: str, value: str) -> str:
    """Ключ «то же значение»: цифры телефона с 8→7, e-mail без регистра, цифры ИНН."""
    if kind == 'TEL':
        digits = re.sub(r'\D', '', value)
        if len(digits) == 11 and digits[0] == '8':
            digits = '7' + digits[1:]
        if len(digits) == 10:
            digits = '7' + digits
        return digits
    if kind == 'INN':
        return re.sub(r'\D', '', value)
    return value.casefold()
