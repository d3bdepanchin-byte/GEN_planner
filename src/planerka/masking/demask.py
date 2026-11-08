"""Расшифровка токенов в ответе LLM (Д-19): токен → ФИО или исходный текст по таблице источника.

Поле с незнакомым токеном становится пустым, заметка «неизвестный токен» — без текста токена:
ответ LLM недоверенный. Токен с кандидатами [Pa|Pb] не выбираем: поле пустое, заметка
«выбрать: ФИО1 или ФИО2» уходит в вопросы кандидата. Заметки остаются на сервере.
"""
from typing import Any

from planerka.masking.tokens import TOKEN_IN_TEXT, TokenStore

UNKNOWN = 'неизвестный токен'


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
