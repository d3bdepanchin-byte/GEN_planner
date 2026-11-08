import tomllib

import pytest

from planerka.core.directory import load_directory
from planerka.db.models import Employee
from planerka.masking.checker import MaskingUncertain, check_residual, ensure_clean
from planerka.masking.masker import Masker
from planerka.masking.names import STOPLIST_PATH, SurnameEdits, load_stoplist


# --- тесты из брифа ---

def test_residual_surname_detected(directory, names):
    assert check_residual('договорились с Кузнецовой', directory, names)


def test_unlisted_variant_blocks_or_is_masked(masker, directory, names):
    r = masker.mask('Санёк Смирнов скинет прайс', 1)
    try:
        ensure_clean(r.text, directory, names)
    except MaskingUncertain:
        return
    assert 'Санёк' not in r.text


def test_masking_uncertain_carries_no_text(directory, names):
    with pytest.raises(MaskingUncertain) as e:
        ensure_clean('Кузнецова', directory, names)
    assert 'Кузнецова' not in str(e.value)


def test_zero_leaks_on_synthetic_reference(masker, directory, names, reference_sentences,
                                           pii_strings):
    for i, s in enumerate(reference_sentences):
        out = masker.mask(s, i).text
        ensure_clean(out, directory, names)
        assert not any(p in out for p in pii_strings)


# --- вторая проверка ---

def test_reference_has_30_phrases(reference_sentences):
    assert len(reference_sentences) == 30


def test_tokens_skipped(directory, names):
    text = 'со [P1] и [P1|P3], клиент [X1], тел. [TEL1], [EMAIL1], ИНН [INN1]'
    assert check_residual(text, directory, names) == []
    ensure_clean(text, directory, names)


def test_residual_spans_point_into_text(directory, names):
    text = '[P1] передал Кузнецовой и Петренко номер +7 913 000-00-00'
    spans = check_residual(text, directory, names)
    found = {text[s.start:s.end] for s in spans}
    assert {'Кузнецовой', 'Петренко', '+7 913 000-00-00'} <= found
    assert all(text[s.start:s.end] == s.text for s in spans)


def test_residual_contacts(directory, names):
    for text in ['звонить 8 (913) 000-00-00', 'пиши a@b.test', 'ИНН 220000000000']:
        assert [s.kind for s in check_residual(text, directory, names)] == [
            {'звонить': 'TEL', 'пиши': 'EMAIL', 'ИНН': 'INN'}[text.split()[0]]], text


def test_residual_variant_lemma_and_lowercase(directory, names):
    assert check_residual('передать сашей', directory, names)
    assert check_residual('звонил петренко', directory, names)
    assert check_residual('звонил клиент Петренко Игорь', directory, names)


def test_clean_business_text_passes(directory, names):
    ensure_clean('ООО «Ромашка» оплатило 1 200 000 руб., отгрузка 05.11.2026', directory, names)


def test_company_name_with_surname_passes(masker, directory, names):
    out = masker.mask('договор с ООО «Петренко и партнёры» на 300 000', 1).text
    ensure_clean(out, directory, names)


def test_masking_uncertain_count(directory, names):
    with pytest.raises(MaskingUncertain) as e:
        ensure_clean('Кузнецова, звонить +7 913 000-00-00', directory, names)
    assert e.value.count == 2
    assert str(e.value) == 'маскирование не уверено: остатков 2'


# --- стоп-лист ---

def test_stoplist_file():
    with open(STOPLIST_PATH, 'rb') as f:
        first = f.readline().decode('utf-8')
    assert first.startswith('#') and 'бренды и слова, которые не являются ПДн' in first
    with open(STOPLIST_PATH, 'rb') as f:
        words = tomllib.load(f)['words']
    assert all(w == w.lower() for w in words)
    assert {'dck', 'bosch', 'бош', 'makita', 'макита', 'aurora', 'аврора', 'кедр', 'hugong',
            'хугонг', 'марка'} <= load_stoplist()


def test_brands_not_masked_and_pass_check(masker, directory, names):
    for text in ['перфораторы Макита и Аврора', 'марка бетона М300',
                 'Bosch, DCK, Кедр и Hugong в наличии', 'перфораторы Макиты и Авроры']:
        out = masker.mask(text, 1).text
        assert out == text
        ensure_clean(out, directory, names)


def test_stoplist_does_not_hide_people(masker):
    assert masker.mask('перфоратор Макита у Петренко', 1).text == 'перфоратор Макита у [X1]'


def test_employee_name_never_suppressed_by_stoplist(session, tokens, names):
    session.add(Employee(full_name='Кедрова Аврора', name_variants=['Кедрова Аврора', 'Аврора']))
    session.flush()
    directory = load_directory(session)
    m = Masker(directory, tokens, names, SurnameEdits.from_settings(session))
    assert m.mask('Аврора пришлёт отчёт', 1).text == '[P4] пришлёт отчёт'
    assert check_residual('Аврора пришлёт отчёт', directory, names)
