"""Контакты регулярными выражениями (Д-16): телефоны, e-mail, ИНН.

Цены, даты и счета не трогаем (Д-21): голое 10- или 12-значное число — ИНН только после
слова «ИНН»; телефон — с +7/7/8 и 10 цифрами номера, мобильный слитно (9 и ещё 9 цифр),
в группировке 3-3-2-2 или городской с дефисами 50-00-00.
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
_PREFIX = r'(?:\+\s*7|7|8)'
_AREA = r'(?:\(\s*\d{3,5}\s*\)|\d{3,5})'
# Телефон с кодом страны или междугородним префиксом: 10 цифр номера проверяются в коде.
_PHONE_FULL = re.compile(
    rf'(?<![\d+]){_PREFIX}{_SEP}{_AREA}{_SEP}\d{{1,3}}(?:{_SEP}\d{{2}}){{2}}(?!\d)')
_PHONE_OTHER = re.compile(
    r'(?<![\d+])(?:\+?7|8)?9\d{9}(?!\d)'                      # мобильный слитно
    # 913 000-00-00
    r'|(?<![\d+])(?<!\d[ \-])\(?\d{3}\)?[ \-]\d{3}[ \-]\d{2}[ \-]\d{2}(?![ \-]?\d)'
    r'|(?<![\d+])\(\s*\d{3,5}\s*\)[ \t]*\d{1,3}-\d{2}-\d{2}(?![\d\-])'  # (3852) 50-00-00
    r'|(?<![\d\-.,+])\d{2,3}-\d{2}-\d{2}(?![\d\-])'         # городской 50-00-00, только дефисы
)
_EMAIL = re.compile(r'[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[^\W\d_]{2,}')
# «ИНН 22…», «ИНН — 22…», «ИНН клиента 22…»: до трёх слов между «ИНН» и номером.
_INN_SEP = r'[\s:№/\-–—]'
_INN = re.compile(
    rf'(?<![^\W\d_])ИНН(?:{_INN_SEP}+[^\W\d_]+){{0,3}}{_INN_SEP}*'
    r'(\d{12}(?!\d)|\d{10}(?!\d)|(?:\d[ \-]?){11}\d(?!\d)|(?:\d[ \-]?){9}\d(?!\d))',
    re.IGNORECASE,
)


def _phones(text: str) -> list[Span]:
    out = [Span(m.start(), m.end(), 'TEL', m.group()) for m in _PHONE_FULL.finditer(text)
           if len(re.sub(r'\D', '', m.group())) == 11]  # префикс + 10 цифр номера
    out += [Span(m.start(), m.end(), 'TEL', m.group()) for m in _PHONE_OTHER.finditer(text)]
    return out


def find_contacts(text: str) -> list[Span]:
    """Спаны контактов по порядку в тексте; у ИНН спан — только цифры, без слова «ИНН»."""
    found: list[Span] = []
    for m in _EMAIL.finditer(text):
        found.append(Span(m.start(), m.end(), 'EMAIL', m.group()))
    for m in _INN.finditer(text):
        found.append(Span(m.start(1), m.end(1), 'INN', m.group(1)))
    found += _phones(text)
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
