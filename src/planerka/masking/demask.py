"""Расшифровка токенов в ответе LLM (Д-19): токен → ФИО или исходный текст по таблице источника.

Поле с незнакомым токеном становится пустым, заметка «неизвестный токен» — без текста токена:
ответ LLM недоверенный. Токен с кандидатами [Pa|Pb] не выбираем: поле пустое, заметка
«выбрать: ФИО1 или ФИО2» уходит в вопросы кандидата. Заметки остаются на сервере.
`demask` — строгий, для полей с человеком; `demask_text` — для свободного текста (R19).
"""
from typing import Any

from planerka.masking.tokens import TOKEN_IN_TEXT, TokenStore

UNKNOWN = 'неизвестный токен'
NOT_IN_RECORDING = 'токена нет в записи'
UNCLEAR = '[?]'  # неразборчивое, как в стенограмме


def demask(data: Any, tokens: TokenStore, source_id: int) -> tuple[Any, list[str]]:
    """Новая структура той же формы (вход не меняется) и заметки без повторов."""
    notes: list[str] = []

    def note(text: str) -> None:
        if text not in notes:
            notes.append(text)

    def field(value: str) -> str:
        out, pos, problems = [], 0, []
        for m in TOKEN_IN_TEXT.finditer(value):
            parts = m.group()[1:-1].split('|')
            resolved = [tokens.resolve(source_id, f'[{p}]') for p in parts]
            if None in resolved or (len(parts) > 1 and not all(p[0] == 'P' for p in parts)):
                problems.append(UNKNOWN)
            elif len(parts) > 1:
                problems.append('выбрать: ' + ' или '.join(resolved))
            else:
                out += [value[pos:m.start()], resolved[0]]
                pos = m.end()
        for p in problems:
            note(p)
        return '' if problems else ''.join(out) + value[pos:]

    def walk(x: Any) -> Any:
        if isinstance(x, str):
            return field(x)
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return type(x)(walk(v) for v in x)
        return x

    return walk(data), notes



def demask_text(text: str, tokens: TokenStore, source_id: int,
                seen: set[str] | None = None) -> tuple[str, list[str]]:
    """Свободный текст (саммари, название, цитата): поле целиком не очищается (R19).

    [Pa|Pb] → «ФИО1 или ФИО2» и заметка «выбрать: …»; незнакомый токен → «[?]» и заметка
    «неизвестный токен»; токен, которого нет в `seen` (токены замаскированной записи), →
    «[?]» и заметка «токена нет в записи». Для полей с человеком — строгий `demask`.
    """
    notes: list[str] = []

    def note(n: str) -> None:
        if n not in notes:
            notes.append(n)

    def sub(m) -> str:
        token = m.group()
        if seen is not None and token not in seen:
            note(NOT_IN_RECORDING)
            return UNCLEAR
        parts = token[1:-1].split('|')
        resolved = [tokens.resolve(source_id, f'[{p}]') for p in parts]
        if None in resolved or (len(parts) > 1 and not all(p[0] == 'P' for p in parts)):
            note(UNKNOWN)
            return UNCLEAR
        if len(parts) > 1:
            note('выбрать: ' + ' или '.join(resolved))
            return ' или '.join(resolved)
        return resolved[0]

    return TOKEN_IN_TEXT.sub(sub, text), notes
