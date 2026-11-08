from pathlib import Path

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


def test_load_env_empty_value_counts_as_unset():
    env = load_env({'DATABASE_URL': 'x', 'SMTP_PORT': ''})
    assert env.smtp_port == 465


def test_load_env_coerces_int_port():
    assert load_env({'DATABASE_URL': 'x', 'SMTP_PORT': '587'}).smtp_port == 587


def test_load_env_default_paths():
    env = load_env({'DATABASE_URL': 'x'})
    assert env.data_dir == Path('data')
    assert env.inbox_dir == Path('data/inbox')
    assert env.backup_dir == Path('backups')
    assert env.models_dir == Path('models')


def test_secrets_not_in_repr():
    env = load_env({
        'DATABASE_URL': 'x', 'ANTHROPIC_API_KEY': 'sk-secret-1', 'HF_TOKEN': 'hf-secret-2',
        'SMTP_PASSWORD': 'pw-secret-3', 'IMAP_PASSWORD': 'pw-secret-4',
        'BACKUP_PASSPHRASE': 'pass-secret-5'})
    text = repr(env)
    for secret in ('sk-secret-1', 'hf-secret-2', 'pw-secret-3', 'pw-secret-4', 'pass-secret-5'):
        assert secret not in text
