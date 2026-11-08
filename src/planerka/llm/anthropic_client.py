"""Клиент Claude API. Классы исключений SDK сверены с установленным пакетом (anthropic 2.6.0)."""
import anthropic

from planerka.llm.base import JsonLLMBase, LLMError, LLMTruncated, LLMUnavailable

TIMEOUT_SEC = 600  # длинные планёрки; повторы делает пайплайн (задача 20), не SDK


class AnthropicClient(JsonLLMBase):
    def __init__(self, api_key: str, model: str, max_tokens: int, max_attempts: int) -> None:
        super().__init__(max_attempts)
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=TIMEOUT_SEC)
        self.model = model
        self.max_tokens = max_tokens

    def _raw_create(self, **kwargs):
        return self._client.messages.create(**kwargs)

    def _send(self, system: str, user: str) -> str:
        try:
            resp = self._raw_create(model=self.model, max_tokens=self.max_tokens, system=system,
                                    messages=[{'role': 'user', 'content': user}])
        except anthropic.APIConnectionError as e:  # включает APITimeoutError
            raise LLMUnavailable(f'соединение с LLM: {type(e).__name__}') from None
        except anthropic.APIStatusError as e:
            code = e.status_code
            if code >= 500 or code == 429:  # 429 лимит, 529 перегрузка, 5xx сбой
                raise LLMUnavailable(f'LLM: статус {code}') from None
            raise LLMError(f'LLM отклонил запрос: статус {code}') from None
        if resp.stop_reason == 'max_tokens':
            raise LLMTruncated()
        return ''.join(b.text for b in resp.content if b.type == 'text')
