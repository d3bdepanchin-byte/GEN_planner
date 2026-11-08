"""Таблица токенов (Д-17, 18): хранится только на сервере, surface — исходный текст.

Сотрудник — постоянный [Pn] (source_id NULL), номер выдаётся по порядку (R17).
Внешний человек [Xn] и контакты [TELn], [EMAILn], [INNn] — нумерация в пределах источника.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.db.models import Employee, Token
from planerka.masking.names import WORD, lemma
from planerka.masking.patterns import normalize_contact

EMPLOYEE, EXTERNAL = 'employee', 'external'
CONTACT_KINDS = ('TEL', 'EMAIL', 'INN')
_TOKEN = re.compile(r'\[([A-Z]+)(\d+)\]')
# Токен в тексте, в том числе с кандидатами: [P1], [P1|P3], [X1], [TEL1], [EMAIL1], [INN1].
TOKEN_IN_TEXT = re.compile(r'\[[A-Z]+\d+(?:\|[A-Z]+\d+)*\]')


def token_number(token: str) -> int:
    m = _TOKEN.fullmatch(token)
    if m is None:
        raise ValueError('не токен')
    return int(m.group(2))


def _person_key(surface: str) -> str:
    """Один человек в пределах источника: совпадают леммы слов («Игорь» = «Игорю»)."""
    return ' '.join(lemma(w) for w in WORD.findall(surface))


class TokenStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def employee(self, emp_id: int) -> str:
        found = self.session.scalars(select(Token.token).where(
            Token.source_id.is_(None), Token.employee_id == emp_id)).first()
        if found is not None:
            return found
        token = self._next('P', None)
        self.session.add(Token(token=token, kind=EMPLOYEE, employee_id=emp_id, source_id=None))
        self.session.flush()
        return token

    def external(self, source_id: int, surface: str) -> str:
        key = _person_key(surface)
        rows = self.session.execute(select(Token.token, Token.surface).where(
            Token.source_id == source_id, Token.kind == EXTERNAL)).all()
        for token, known in rows:
            if _person_key(known or '') == key:
                return token
        return self._add(source_id, 'X', EXTERNAL, surface)

    def contact(self, source_id: int, kind: str, value: str) -> str:
        if kind not in CONTACT_KINDS:
            raise ValueError('неизвестный вид контакта')
        key = normalize_contact(kind, value)
        rows = self.session.execute(select(Token.token, Token.surface).where(
            Token.source_id == source_id, Token.kind == kind.lower())).all()
        for token, known in rows:
            if normalize_contact(kind, known or '') == key:
                return token
        return self._add(source_id, kind, kind.lower(), value)

    def resolve(self, source_id: int, token: str) -> str | None:
        """ФИО сотрудника для [Pn] (глобально), исходный текст для остальных (в источнике)."""
        m = _TOKEN.fullmatch(token)
        if m is None:
            return None
        if m.group(1) == 'P':
            return self.session.scalars(
                select(Employee.full_name).join(Token, Token.employee_id == Employee.id)
                .where(Token.source_id.is_(None), Token.token == token)).first()
        return self.session.scalars(select(Token.surface).where(
            Token.source_id == source_id, Token.token == token)).first()

    def _add(self, source_id: int, prefix: str, kind: str, surface: str) -> str:
        token = self._next(prefix, source_id)
        self.session.add(Token(token=token, kind=kind, source_id=source_id, surface=surface))
        self.session.flush()
        return token

    def _next(self, prefix: str, source_id: int | None) -> str:
        where = Token.source_id.is_(None) if source_id is None else Token.source_id == source_id
        numbers = [int(m.group(2)) for t in self.session.scalars(select(Token.token).where(where))
                   if (m := _TOKEN.fullmatch(t)) and m.group(1) == prefix]
        return f'[{prefix}{max(numbers, default=0) + 1}]'
