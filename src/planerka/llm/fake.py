"""Фейковый LLM для тестов: ответы по очереди, запросы записываются."""
from planerka.llm.base import JsonLLMBase


class FakeLLM(JsonLLMBase):
    def __init__(self, responses: list[str], max_attempts: int = 3) -> None:
        super().__init__(max_attempts)
        self._responses = list(responses)
        self.requests: list[str] = []

    def _send(self, system: str, user: str) -> str:
        self.requests.append(user)
        if not self._responses:
            raise RuntimeError('FakeLLM: ответы закончились')
        return self._responses.pop(0)
