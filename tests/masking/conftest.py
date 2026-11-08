from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'

# Справочник, токены и маскировщик — в tests/conftest.py (общие с tests/extract).


def _lines(name: str) -> list[str]:
    """Строки файла без пустых и комментариев «#»."""
    text = (FIXTURES / name).read_text(encoding='utf-8')
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith('#')]


@pytest.fixture(scope='session')
def reference_sentences():
    """30 синтетических фраз со всеми видами ПДн."""
    return _lines('pii_reference.txt')


@pytest.fixture(scope='session')
def pii_strings():
    """ПДн из эталонных фраз: ни одна не должна остаться в замаскированном тексте."""
    return _lines('pii_reference_strings.txt')
