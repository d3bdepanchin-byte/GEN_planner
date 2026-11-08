from planerka.masking.demask import demask


# --- тесты из брифа ---

def test_demask_replaces_known_tokens(tokens):
    data, notes = demask({'owner': '[P1]', 'quote': 'сделает [P1]'}, tokens, 1)
    assert data == {'owner': 'Смирнов Александр', 'quote': 'сделает Смирнов Александр'}
    assert notes == []


def test_unknown_token_empties_field_and_notes(tokens):
    data, notes = demask({'owner': '[P99]'}, tokens, 1)
    assert data['owner'] == '' and notes


def test_ambiguous_token_left_for_human_choice(tokens):
    data, notes = demask({'owner': '[P1|P3]'}, tokens, 1)
    assert data['owner'] == '' and any('выбрать' in n for n in notes)


# --- обход и заметки ---

def test_ambiguous_note_names_candidates(tokens):
    _, notes = demask({'owner': '[P1|P3]'}, tokens, 1)
    assert notes == ['выбрать: Смирнов Александр или Попов Александр']


def test_unknown_token_note_has_no_token_text(tokens):
    data, notes = demask({'quote': 'позвонить [X7] и [P1]'}, tokens, 1)
    assert data['quote'] == ''
    assert notes == ['неизвестный токен']


def test_ambiguous_with_unknown_candidate_is_unknown(tokens):
    data, notes = demask({'owner': '[P1|P99]'}, tokens, 1)
    assert data['owner'] == '' and notes == ['неизвестный токен']


def test_source_tokens_resolved_only_in_their_source(tokens):
    x = tokens.external(1, 'Петренко Игорь')
    tel = tokens.contact(1, 'TEL', '+7 913 000-00-00')
    data, notes = demask({'quote': f'{x}, тел. {tel}'}, tokens, 1)
    assert data == {'quote': 'Петренко Игорь, тел. +7 913 000-00-00'} and notes == []
    data, notes = demask({'quote': f'{x}'}, tokens, 2)
    assert data == {'quote': ''} and notes == ['неизвестный токен']


def test_nested_structure_walked_without_mutation(tokens):
    src = {'tasks': [{'owner': '[P2]', 'co': ['[P1]', '[P1|P3]'], 'priority': 2,
                      'done': None, 'ok': True}],
           'title': 'без токенов'}
    data, notes = demask(src, tokens, 1)
    assert data == {'tasks': [{'owner': 'Кузнецова Ольга', 'co': ['Смирнов Александр', ''],
                               'priority': 2, 'done': None, 'ok': True}],
                    'title': 'без токенов'}
    assert notes == ['выбрать: Смирнов Александр или Попов Александр']
    assert src['tasks'][0]['owner'] == '[P2]' and src['tasks'][0]['co'][1] == '[P1|P3]'


def test_repeated_problem_noted_once(tokens):
    _, notes = demask(['[P1|P3]', 'и [P1|P3]', '[P99]', '[X5]'], tokens, 1)
    assert notes == ['выбрать: Смирнов Александр или Попов Александр', 'неизвестный токен']


def test_non_token_brackets_kept(tokens):
    data, notes = demask({'quote': 'срок [?] и [п1]'}, tokens, 1)
    assert data == {'quote': 'срок [?] и [п1]'} and notes == []
