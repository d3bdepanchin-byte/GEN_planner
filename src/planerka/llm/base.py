"""Общая логика LLM-адаптера: протокол клиента, ошибки, JsonLLMBase."""
import re
from typing import Protocol

from pydantic import BaseModel, ValidationError

from planerka.llm.prompts import Prompt


class LLMError(Exception):
    """Ошибка LLM-слоя. Сообщения без текста запроса и ответа (там замаскированный текст встречи)."""


class LLMUnavailable(LLMError):
    """Сервис недоступен: соединение, таймаут, 429, 5xx, 529. Повтор позже — политика пайплайна."""


class LLMSchemaError(LLMError):
    """Ответ не прошёл проверку схемы за все попытки."""


class LLMTruncated(Exception):
    """Ответ оборван по лимиту токенов. Внутренний сигнал _send → complete_json, наружу не выходит."""


class LLMClient(Protocol):
    def complete_json(self, prompt: Prompt, variables: dict[str, str],
                      schema: type[BaseModel]) -> BaseModel: ...


_FENCE = re.compile(r'^```[A-Za-z]*[ \t]*\n?(.*?)\n?```$', re.DOTALL)


def strip_fence(raw: str) -> str:
    """Убирает пробелы по краям и обрамление ```json ... ``` / ``` ... ```."""
    s = raw.strip()
    m = _FENCE.match(s)
    return m.group(1).strip() if m else s


def _error_text(e: ValidationError) -> str:
    """Ошибки схемы без входных значений (в них был бы текст ответа): путь, сообщение, тип."""
    return '\n'.join(f"- {'.'.join(map(str, d['loc'])) or '(корень)'}: {d['msg']} [{d['type']}]"
                     for d in e.errors(include_input=False, include_url=False, include_context=False))


class JsonLLMBase:
    def __init__(self, max_attempts: int = 3) -> None:
        self.max_attempts = max_attempts

    def _send(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, prompt: Prompt, variables: dict[str, str],
                      schema: type[BaseModel]) -> BaseModel:
        """Запрос, разбор и проверка по схеме.

        При неудаче повторный запрос одним сообщением пользователя: исходный текст,
        предыдущий ответ и ошибка проверки (для обрыва по токенам: просьба ответить короче,
        обрезанный ответ не повторяется). Не больше max_attempts, затем LLMSchemaError.
        LLMUnavailable/LLMError из _send не перехватываются: повтор при сбое сервиса — задача 20.
        """
        base = prompt.render(variables)  # KeyError до запроса, если нет переменной
        user = base
        for _ in range(self.max_attempts):
            try:
                raw = self._send(prompt.system, user)
            except LLMTruncated:
                user = (f'{base}\n\nПредыдущий ответ оборван по лимиту длины. '
                        'Верни тот же JSON целиком, но короче: сократи формулировки.')
                continue
            try:
                return schema.model_validate_json(strip_fence(raw))
            except ValidationError as e:
                user = (f'{base}\n\nТвой предыдущий ответ:\n{raw}\n\n'
                        f'Он не прошёл проверку:\n{_error_text(e)}\n'
                        'Верни исправленный JSON целиком, без пояснений.')
        raise LLMSchemaError(f'ответ LLM не прошёл проверку схемы за {self.max_attempts} попыт.')
