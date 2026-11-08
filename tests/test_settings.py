import pytest
from planerka.settings import load_env, load_defaults

def test_defaults_flattened():
    d = load_defaults()
    assert d['review.return_workdays'] == 2
    assert d['intake.opening_phrases.planerka'] == ['начинаем планёрку']

def test_goals_2027_from_spec():
    assert load_defaults()['goals.goals_2027'] == [
        'стратегия продаж', 'сайт и маркетинг', 'ассортимент',
        'автоматизация и ИИ', 'эффективность', 'клиентская база', 'нет связи']

def test_defaults_contain_no_secrets():
    secret_words = {'key', 'token', 'password', 'passphrase'}
    for key, value in load_defaults().items():
        # целые слова: llm.max_tokens (лимит токенов) — не секрет
        if secret_words & set(key.replace('.', '_').split('_')):
            assert not value, key

def test_missing_required_env_raises():
    with pytest.raises(ValueError):
        load_env({})
