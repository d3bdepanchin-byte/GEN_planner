from pathlib import Path

import pytest

from planerka.llm.prompts import Prompt, PromptVariableError, load_prompt

FIXTURES = Path(__file__).parent.parent / 'fixtures'


def test_load_splits_system_and_template():
    p = load_prompt('echo', 1, FIXTURES / 'prompts')
    assert (p.name, p.version) == ('echo', 1)
    assert p.system.startswith('Ты возвращаешь ответ')
    assert '---USER---' not in p.system + p.template
    assert '$text' in p.template


def test_missing_file_is_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_prompt('echo', 2, FIXTURES / 'prompts')


def test_missing_separator_is_value_error(tmp_path):
    (tmp_path / 'bad.v1.md').write_text('без разделителя', encoding='utf-8')
    with pytest.raises(ValueError):
        load_prompt('bad', 1, tmp_path)


def test_render_keeps_json_braces_and_dollar_in_values():
    p = Prompt('t', 1, 's', 'Пример {"a": 1}. Текст: $text')
    assert p.render({'text': 'цена $5 и ${x}'}) == 'Пример {"a": 1}. Текст: цена $5 и ${x}'


def test_missing_variable_names_variable_not_text():
    p = Prompt('t', 1, 's', 'Привет $who, $text')
    with pytest.raises(PromptVariableError) as e:
        p.render({'text': 'секретный текст встречи'})
    assert isinstance(e.value, KeyError)
    assert 'who' in str(e.value)
    assert 'секретный' not in str(e.value)
