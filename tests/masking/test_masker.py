from planerka.masking.names import SurnameEdits, lemma, match_employees
from planerka.masking.patterns import find_contacts

EDITS = SurnameEdits(max_short=1, max_long=2, short_from=5, long_from=9)


# --- тесты из брифа ---

def test_variants_get_stable_token(masker):
    r = masker.mask('Александр Смирнов и Смирнов договорились', source_id=1)
    assert r.text.count('[P1]') == 2


def test_same_employee_same_token_across_sources(masker):
    assert '[P2]' in masker.mask('Ольга Кузнецова', 1).text
    assert '[P2]' in masker.mask('Кузнецова', 2).text


def test_inflected_surname_matched(masker):
    assert masker.mask('договорились со Смирновым', 1).text == 'договорились со [P1]'


def test_ambiguous_first_name_gets_candidates_token(masker):
    assert '[P1|P3]' in masker.mask('Саша возьмёт на себя', 1).text


def test_external_person_masked_per_source(masker):
    r = masker.mask('звонил клиент Петренко Игорь', 1)
    assert 'Петренко' not in r.text and '[X1]' in r.text


def test_contacts_masked(masker):
    r = masker.mask('тел. +7 913 000-00-00, a@b.test, ИНН 220000000000', 1)
    assert {'[TEL1]', '[EMAIL1]', '[INN1]'} <= r.tokens


def test_ip_masked_company_and_prices_kept(masker):
    r = masker.mask('ИП Волков и ООО «Ромашка» по цене 1 200 000', 1)
    assert 'Волков' not in r.text
    assert 'ООО «Ромашка»' in r.text and '1 200 000' in r.text


# --- контакты ---

def _kinds(text):
    return [(s.kind, s.text) for s in find_contacts(text)]


def test_phone_formats():
    for phone in ['+7 913 000-00-00', '8 (913) 000-00-00', '+7(913)0000000',
                  '8-913-000-00-00', '89130000000', '+79130000000', '913 000-00-00',
                  '7 913 000-00-00', '79130000000', '9130000000', '7 (913) 000 00 00',
                  '8 (3852) 50-00-00', '+7 3852 50-00-00', '50-00-00', '8 913 000 0000',
                  '8 800 000-00-00']:
        assert _kinds(f'звони {phone} завтра') == [('TEL', phone)], phone


def test_email_and_inn():
    assert _kinds('пиши на ivan.petrov@example.test') == [('EMAIL', 'ivan.petrov@example.test')]
    assert _kinds('ИНН 2200000000') == [('INN', '2200000000')]
    assert _kinds('инн: 220000000000.') == [('INN', '220000000000')]
    assert _kinds('ИНН — 220000000000') == [('INN', '220000000000')]
    assert _kinds('ИНН клиента 220000000000') == [('INN', '220000000000')]
    assert _kinds('его ИНН 2200-0000-0000') == [('INN', '2200-0000-0000')]
    assert _kinds('инн 22 00 00 00 00') == [('INN', '22 00 00 00 00')]


def test_bare_numbers_and_prices_not_contacts():
    assert find_contacts('номер 220000000000 и оборот 1200000000') == []
    assert find_contacts('цена 1 200 000, оборот 12 500 000 000 руб.') == []
    assert find_contacts('цена 1 200 000 рублей, дата 05.11.2026, оборот 12 500 000') == []
    assert find_contacts('счёт 40702810000000000000 от 05.11.2026') == []
    assert find_contacts('оборот 8 500 000 000 и 7 500 000 руб.') == []


def test_same_contact_same_token_in_source(masker):
    r = masker.mask('звони 8 913 000-00-00 или +7 913 000 00 00, запасной 8 913 111-11-11', 1)
    assert r.text == 'звони [TEL1] или [TEL1], запасной [TEL2]'


def test_inn_prefix_kept(masker):
    assert masker.mask('ИНН 2200000000', 1).text == 'ИНН [INN1]'
    assert masker.mask('инн 22 00 00 00 00', 2).text == 'инн [INN1]'


# --- сотрудники ---

def test_lemma_of_inflected_surname():
    assert lemma('Смирновым') == 'смирнов'


def test_multiword_variant_is_one_match(directory):
    m = match_employees('Александр Смирнов придёт', directory, EDITS)
    assert [(x.span.text, len(x.employee_ids)) for x in m] == [('Александр Смирнов', 1)]


def test_ambiguous_match_lists_all_candidates(directory):
    smirnov, popov = directory[0].id, directory[2].id
    m = match_employees('Сашей', directory, EDITS)
    assert len(m) == 1 and sorted(m[0].employee_ids) == sorted([smirnov, popov])


def test_distorted_surname_matched_fuzzy(masker):
    assert masker.mask('Кузницова пришлёт отчёт', 1).text == '[P2] пришлёт отчёт'


def test_distorted_surnames_short_and_inflected(masker):
    assert masker.mask('Смирнав пришлёт', 1).text == '[P1] пришлёт'
    assert masker.mask('договорились со Смирнавым', 1).text == 'договорились со [P1]'
    assert masker.mask('Смерновым договорились', 1).text == '[P1] договорились'
    assert masker.mask('Попав сделает', 1).text == '[P3] сделает'
    assert masker.mask('Кузнитсова пришлёт', 1).text == '[P2] пришлёт'
    assert masker.mask('Смирнофф пришлёт', 1).text == '[P1] пришлёт'
    assert masker.mask('смирнав пришлёт', 1).text == '[P1] пришлёт'


def test_surname_edits_from_settings(session):
    assert SurnameEdits.from_settings(session) == EDITS


def test_lowercase_dictionary_word_not_fuzzy(masker):
    assert masker.mask('сидеть смирно', 1).text == 'сидеть смирно'


def test_first_names_not_fuzzy(directory):
    assert match_employees('Олга', directory, EDITS) == []


def test_ambiguous_reported(masker, directory):
    r = masker.mask('Саша возьмёт, Саша сделает', 1)
    assert r.ambiguous == [[directory[0].id, directory[2].id]]
    assert '[P1|P3]' in r.tokens


def test_unambiguous_has_no_candidates(masker):
    assert masker.mask('Оля и Смирнов', 1).ambiguous == []


# --- внешние люди, ИП, компании ---

def test_external_same_person_same_token_within_source(masker):
    a = masker.mask('звонил Петренко Игорь', 3)
    b = masker.mask('перезвонить Петренко Игорю', 3)
    c = masker.mask('звонил Петренко Игорь', 4)
    assert '[X1]' in a.text and '[X1]' in b.text and '[X1]' in c.text
    d = masker.mask('и ещё Васильев Олег', 3)
    assert '[X2]' in d.text and 'Васильев' not in d.text


def test_ip_with_initials(masker):
    assert masker.mask('счёт от ИП Волков И. П. пришёл', 1).text == 'счёт от ИП [X1] пришёл'


def test_ip_lowercase_asr_output(masker):
    assert masker.mask('ип волков по цене 500 000', 1).text == 'ип [X1] по цене 500 000'
    assert masker.mask('ип и ооо', 1).text == 'ип и ооо'


def test_lowercase_people_masked(masker):
    assert masker.mask('звонил клиент петренко игорь', 1).text == 'звонил клиент [X1]'
    assert masker.mask('клиент марина белова просила скидку', 2).text == 'клиент [X1] просила скидку'
    assert masker.mask('у петренко долг 300 000', 3).text == 'у [X1] долг 300 000'
    assert masker.mask('перезвонить сидорову ивану завтра', 4).text == 'перезвонить [X1] завтра'
    assert masker.mask('ира из бухгалтерии звонила', 5).text == '[X1] из бухгалтерии звонила'


def test_ip_lowercase_full_name(masker):
    assert masker.mask('ип волкова анна сергеевна', 1).text == 'ип [X1]'
    assert masker.mask('ип анна волкова', 2).text == 'ип [X1]'


def test_ip_initials_before_surname(masker):
    assert masker.mask('ИП А.С. Волков', 1).text == 'ИП [X1]'


def test_company_name_with_surname_kept(masker):
    r = masker.mask('договор с ООО «Петренко и партнёры» на 300 000', 1)
    assert 'ООО «Петренко и партнёры»' in r.text and '300 000' in r.text


# --- таблица токенов ---

def test_employee_token_permanent_and_sequential(session, tokens, directory):
    assert tokens.employee(directory[1].id) == '[P2]'
    assert tokens.employee(directory[0].id) == '[P1]'


def test_resolve(tokens, directory):
    x = tokens.external(1, 'Петренко Игорь')
    tel = tokens.contact(1, 'TEL', '+7 913 000-00-00')
    assert tokens.resolve(1, '[P1]') == 'Смирнов Александр'
    assert tokens.resolve(5, '[P1]') == 'Смирнов Александр'
    assert tokens.resolve(1, x) == 'Петренко Игорь'
    assert tokens.resolve(1, tel) == '+7 913 000-00-00'
    assert tokens.resolve(2, x) is None
    assert tokens.resolve(1, '[P99]') is None
    assert tokens.resolve(1, '[X9]') is None
