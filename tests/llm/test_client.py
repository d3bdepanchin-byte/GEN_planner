import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
from pydantic import BaseModel

from planerka.llm.anthropic_client import AnthropicClient
from planerka.llm.base import LLMError, LLMSchemaError, LLMUnavailable
from planerka.llm.fake import FakeLLM
from planerka.llm.prompts import load_prompt

FIXTURES = Path(__file__).parent.parent / 'fixtures'
FENCE = chr(96) * 3


class Out(BaseModel):
    answer: str


PROMPT = load_prompt('echo', 1, FIXTURES / 'prompts')
OK = json.dumps({'answer': 'ok'})
REQ = httpx.Request('POST', 'https://api.example.test/v1/messages')


def raise_connection_error(**kwargs):
    raise anthropic.APIConnectionError(request=REQ)


def status_error(code):
    def raiser(**kwargs):
        resp = httpx.Response(code, request=REQ, json={'error': {'message': 'секрет [P1]'}})
        raise anthropic.APIStatusError('секрет [P1]', response=resp, body=None)
    return raiser


def response(*blocks, stop_reason='end_turn'):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def text(t):
    return SimpleNamespace(type='text', text=t)


def client_with(monkeypatch, *outcomes, attempts=3):
    """Клиент, у которого _raw_create отдаёт ответы по очереди; kwargs запросов пишутся в .calls."""
    client = AnthropicClient('k', 'm', 100, attempts)
    queue = list(outcomes)
    client.calls = []

    def fake_create(**kwargs):
        client.calls.append(kwargs)
        return queue.pop(0)
    monkeypatch.setattr(client, '_raw_create', fake_create)
    return client


def test_prompt_loaded_with_version():
    assert (PROMPT.name, PROMPT.version) == ('echo', 1)
    assert '$text' in PROMPT.template


def test_valid_json_parsed():
    assert FakeLLM([OK]).complete_json(PROMPT, {'text': 'x'}, Out).answer == 'ok'


def test_code_fence_stripped():
    llm = FakeLLM([FENCE + 'json' + chr(10) + OK + chr(10) + FENCE])
    assert llm.complete_json(PROMPT, {'text': 'x'}, Out).answer == 'ok'


def test_bare_fence_and_whitespace_stripped():
    llm = FakeLLM(['  \n' + FENCE + chr(10) + OK + chr(10) + FENCE + '\n  '])
    assert llm.complete_json(PROMPT, {'text': 'x'}, Out).answer == 'ok'


def test_invalid_json_retried_then_schema_error():
    llm = FakeLLM(['not json'] * 3)
    with pytest.raises(LLMSchemaError):
        llm.complete_json(PROMPT, {'text': 'x'}, Out)
    assert len(llm.requests) == 3


def test_schema_error_message_has_attempts_only():
    llm = FakeLLM(['{"answer": "СЕКРЕТ [P1]", "extra": ' + '"x"'] * 2, max_attempts=2)
    with pytest.raises(LLMSchemaError) as e:
        llm.complete_json(PROMPT, {'text': 'встреча [P1]'}, Out)
    assert '2' in str(e.value)
    assert 'СЕКРЕТ' not in str(e.value) and '[P1]' not in str(e.value)


def test_retry_after_invalid_succeeds_and_request_has_context():
    llm = FakeLLM(['{"wrong": 1}', OK])
    assert llm.complete_json(PROMPT, {'text': 'x'}, Out).answer == 'ok'
    assert len(llm.requests) == 2
    retry = llm.requests[1]
    assert llm.requests[0] in retry          # исходный текст запроса
    assert '{"wrong": 1}' in retry           # предыдущий ответ
    assert 'answer' in retry                 # текст ошибки валидации (поле)


def test_retry_error_text_does_not_echo_input_value():
    llm = FakeLLM(['{"answer": 123456789}', OK])
    llm.complete_json(PROMPT, {'text': 'x'}, Out)
    tail = llm.requests[1][len(llm.requests[0]):]
    assert 'input_value' not in tail


def test_max_attempts_one_makes_one_request():
    llm = FakeLLM(['bad'], max_attempts=1)
    with pytest.raises(LLMSchemaError):
        llm.complete_json(PROMPT, {'text': 'x'}, Out)
    assert len(llm.requests) == 1


def test_request_contains_rendered_text():
    llm = FakeLLM([OK])
    llm.complete_json(PROMPT, {'text': 'привет [P1]'}, Out)
    assert 'привет [P1]' in llm.requests[0]


def test_missing_variable_raises_before_any_request():
    llm = FakeLLM([OK])
    with pytest.raises(KeyError):
        llm.complete_json(PROMPT, {}, Out)
    assert llm.requests == []


def test_connection_error_maps_to_unavailable(monkeypatch):
    client = AnthropicClient('k', 'm', 100, 3)
    monkeypatch.setattr(client, '_raw_create', raise_connection_error)
    with pytest.raises(LLMUnavailable):
        client.complete_json(PROMPT, {'text': 'x'}, Out)


def test_timeout_maps_to_unavailable(monkeypatch):
    def raiser(**kwargs):
        raise anthropic.APITimeoutError(request=REQ)
    client = AnthropicClient('k', 'm', 100, 3)
    monkeypatch.setattr(client, '_raw_create', raiser)
    with pytest.raises(LLMUnavailable):
        client.complete_json(PROMPT, {'text': 'x'}, Out)


@pytest.mark.parametrize('code', [429, 500, 502, 503, 529])
def test_outage_statuses_map_to_unavailable(monkeypatch, code):
    client = AnthropicClient('k', 'm', 100, 3)
    monkeypatch.setattr(client, '_raw_create', status_error(code))
    with pytest.raises(LLMUnavailable) as e:
        client.complete_json(PROMPT, {'text': 'x'}, Out)
    assert str(code) in str(e.value)
    assert 'секрет' not in str(e.value) and '[P1]' not in str(e.value)


@pytest.mark.parametrize('code', [400, 401, 403, 404])
def test_client_errors_are_llm_error_not_unavailable(monkeypatch, code):
    client = AnthropicClient('k', 'm', 100, 3)
    monkeypatch.setattr(client, '_raw_create', status_error(code))
    with pytest.raises(LLMError) as e:
        client.complete_json(PROMPT, {'text': 'x'}, Out)
    assert not isinstance(e.value, LLMUnavailable)
    assert str(code) in str(e.value)
    assert 'секрет' not in str(e.value)


def test_error_hierarchy():
    assert issubclass(LLMUnavailable, LLMError) and issubclass(LLMSchemaError, LLMError)


def test_send_passes_params_and_joins_text_blocks(monkeypatch):
    client = client_with(monkeypatch, response(
        text('{"answer": '), SimpleNamespace(type='thinking', thinking='...'), text('"ok"}')))
    assert client.complete_json(PROMPT, {'text': 'привет'}, Out).answer == 'ok'
    call = client.calls[0]
    assert call['model'] == 'm' and call['max_tokens'] == 100
    assert call['system'] == PROMPT.system
    assert call['messages'] == [{'role': 'user', 'content': PROMPT.render({'text': 'привет'})}]


def test_max_tokens_stop_is_failed_attempt_then_retried(monkeypatch):
    client = client_with(monkeypatch,
                         response(text('{"answer": "об'), stop_reason='max_tokens'),
                         response(text(OK)))
    assert client.complete_json(PROMPT, {'text': 'x'}, Out).answer == 'ok'
    retry = client.calls[1]['messages'][0]['content']
    assert 'оборван' in retry and 'короче' in retry


def test_max_tokens_every_attempt_is_schema_error(monkeypatch):
    # даже если обрезанный кусок случайно валиден, это не успех
    client = client_with(monkeypatch, *[response(text(OK), stop_reason='max_tokens')] * 3)
    with pytest.raises(LLMSchemaError):
        client.complete_json(PROMPT, {'text': 'x'}, Out)
    assert len(client.calls) == 3


def test_sdk_client_has_no_sdk_retries_and_timeout():
    client = AnthropicClient('k', 'm', 100, 3)
    assert client._client.max_retries == 0
    assert client._client.timeout == 600


def test_timeout_is_parameter_reaching_sdk_client():
    client = AnthropicClient('k', 'm', 100, 3, timeout_sec=42)
    assert client._client.timeout == 42


def test_default_timeout_matches_settings_default():
    import tomllib
    cfg = tomllib.loads((Path(__file__).parents[2] / 'config' / 'defaults.toml').read_text(encoding='utf-8'))
    assert cfg['llm']['timeout_sec'] == 600
    assert AnthropicClient('k', 'm', 100, 3)._client.timeout == cfg['llm']['timeout_sec']


def test_other_sdk_errors_become_llm_error_without_body(monkeypatch):
    def raiser(**kwargs):
        resp = httpx.Response(200, request=REQ, text='секрет [P1]')
        raise anthropic.APIResponseValidationError(response=resp, body='секрет [P1]')
    client = AnthropicClient('k', 'm', 100, 3)
    monkeypatch.setattr(client, '_raw_create', raiser)
    with pytest.raises(LLMError) as e:
        client.complete_json(PROMPT, {'text': 'x'}, Out)
    assert not isinstance(e.value, LLMUnavailable)
    assert str(e.value) == 'APIResponseValidationError'
    assert e.value.__cause__ is None and e.value.__suppress_context__
