"""Имена: леммы natasha, распознаватель имён (NER) и сопоставление со справочником (Д-16, 17)."""
import re
import tomllib
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from rapidfuzz.distance import Levenshtein

from planerka.core.directory import EmployeeRef, norm_name
from planerka.masking.patterns import Span
from planerka.settings import PROJECT_ROOT, get_setting

WORD = re.compile(r'[^\W\d_]+(?:-[^\W\d_]+)*')
STOPLIST_PATH = PROJECT_ROOT / 'config' / 'masking_stoplist.toml'


class NameFinder(Protocol):
    def find(self, text: str) -> list[Span]: ...


class NatashaNameFinder:
    """Имена людей (спаны вида PER) по natasha. Создавать один раз: загрузка небыстрая."""

    def __init__(self) -> None:
        from natasha import NewsEmbedding, NewsNERTagger, Segmenter

        self._segmenter = Segmenter()
        self._ner = NewsNERTagger(NewsEmbedding())

    def find(self, text: str) -> list[Span]:
        from natasha import Doc

        doc = Doc(text)
        doc.segment(self._segmenter)
        doc.tag_ner(self._ner)
        return [Span(s.start, s.stop, 'PER', text[s.start:s.stop])
                for s in doc.spans if s.type == 'PER']


@lru_cache(maxsize=1)
def _morph():
    from natasha import MorphVocab

    # pymorphy2 внутри natasha импортирует устаревший pkg_resources — шум чужого пакета.
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='pkg_resources is deprecated',
                                category=DeprecationWarning)
        return MorphVocab()


@lru_cache(maxsize=100_000)
def lemmas(word: str) -> frozenset[str]:
    """Все нормальные формы слова по словарю natasha плюс само слово — ключи сравнения."""
    forms = {norm_name(f.normal) for f in _morph()(word)}
    forms.add(norm_name(word))
    return frozenset(forms)


def lemma(word: str) -> str:
    """Самая вероятная нормальная форма (без регистра, ё→е): «Смирновым» → «смирнов»."""
    forms = _morph()(word)
    return norm_name(forms[0].normal if forms else word)


_PERSON_GRAMMEMES = ('Name', 'Surn', 'Patr')


@lru_cache(maxsize=100_000)
def person_like(word: str) -> bool:
    """Самый вероятный разбор (или один из равных) — имя, фамилия, отчество («петренко», «ира»)."""
    parses = _morph().parse(word)
    if len(word) <= 4 and (word.isupper() or any('Abbr' in p.tag for p in parses)):
        return False  # аббревиатуры: «ИНН», «инн» разбираются как «Инна»
    top = [p for p in parses if p.score >= parses[0].score] if parses else []
    return any(g in p.tag for p in top for g in _PERSON_GRAMMEMES)  # при равных — любой


@lru_cache(maxsize=100_000)
def _common_lowercase(word: str) -> bool:
    """Строчное словарное слово, у которого нет разбора как имени: «смирно», «белое»."""
    parses = _morph().parse(word)
    return (word == word.lower() and any(p.is_known for p in parses)
            and not any(g in p.tag for p in parses for g in _PERSON_GRAMMEMES))


@dataclass(frozen=True)
class SurnameEdits:
    """Допуск правок (расстояние Левенштейна) в фамилии: распознавание искажает фамилии (Д-26).

    Фамилия короче short_from — только точное совпадение леммы.
    """
    max_short: int
    max_long: int
    short_from: int
    long_from: int

    @classmethod
    def from_settings(cls, session) -> 'SurnameEdits':
        return cls(max_short=get_setting(session, 'masking.surname_max_edits_short'),
                   max_long=get_setting(session, 'masking.surname_max_edits_long'),
                   short_from=get_setting(session, 'masking.surname_short_from'),
                   long_from=get_setting(session, 'masking.surname_long_from'))

    def allowed(self, surname: str) -> int:
        if len(surname) < self.short_from:
            return 0
        return self.max_long if len(surname) >= self.long_from else self.max_short


# Окончания падежей фамилий и прилагательных: «Смирнавым» → основа «смирнав».
_ENDINGS = ('ыми', 'ими', 'ого', 'его', 'ому', 'ему', 'ым', 'им', 'ой', 'ей', 'ом', 'ем',
            'ую', 'ых', 'их', 'а', 'у', 'е', 'ы', 'ю', 'я')


def _phonetic(form: str) -> str:
    """Типичные искажения распознавания: «тс» вместо «ц», удвоенные буквы («Смирнофф»)."""
    return re.sub(r'(.)\1+', r'\1', form.replace('тс', 'ц'))


@lru_cache(maxsize=100_000)
def _surname_forms(word: str) -> frozenset[str]:
    """Леммы и основы без падежного окончания, после фонетической нормализации."""
    forms = set(lemmas(word))
    w = norm_name(word)
    forms.update(w[:-len(e)] for e in _ENDINGS if w.endswith(e) and len(w) - len(e) >= 3)
    return frozenset(_phonetic(f) for f in forms)


@dataclass(frozen=True)
class EmployeeMatch:
    span: Span
    employee_ids: list[int]


@dataclass(frozen=True)
class _Candidate:
    first: int  # индекс первого слова текста
    last: int   # индекс последнего слова, включительно
    employee_id: int
    score: float


_VKey = tuple[str, bool]  # слово варианта и признак «это фамилия сотрудника»


def _word_score(word: str, vword: str, is_surname: bool, edits: SurnameEdits) -> float:
    """100 — точная лемма; для фамилии 100 минус число правок в пределах допуска; иначе 0."""
    if lemmas(word) & lemmas(vword):
        return 100.0
    if not is_surname or _common_lowercase(word):  # имена и уменьшительные — только точно
        return 0.0
    limit = edits.allowed(norm_name(vword))
    if not limit or len(word) < edits.short_from - limit:  # «по» не искажённый «Попов»
        return 0.0
    best = min(Levenshtein.distance(x, y, score_cutoff=limit)
               for x in _surname_forms(word) for y in _surname_forms(vword))
    return 100.0 - best if best <= limit else 0.0


def _variant_keys(emp: EmployeeRef) -> set[tuple[_VKey, ...]]:
    parts = emp.full_name.split()
    surname = norm_name(parts[0]) if parts else ''
    return {tuple((w, norm_name(w) == surname) for w in v.split())
            for v in emp.variants if v.split()}


def _candidates(words: list[str], directory: list[EmployeeRef],
                edits: SurnameEdits) -> list[_Candidate]:
    variants = {emp.id: _variant_keys(emp) for emp in directory}
    vkeys = {k for vs in variants.values() for v in vs for k in v}
    # Каждое различное слово текста сравниваем с каждым словом вариантов один раз.
    scores: dict[str, dict[_VKey, float]] = {}
    positions: dict[_VKey, list[int]] = {}
    for i, word in enumerate(words):
        if word not in scores:
            scores[word] = {k: sc for k in vkeys
                            if (sc := _word_score(word, k[0], k[1], edits))}
        for k in scores[word]:
            positions.setdefault(k, []).append(i)
    out = []
    for emp_id, emp_variants in variants.items():
        for variant in emp_variants:
            n = len(variant)
            for i in positions.get(variant[0], []):
                if i + n > len(words):
                    continue
                score = min(scores[words[i + j]].get(k, 0.0) for j, k in enumerate(variant))
                if score:
                    out.append(_Candidate(i, i + n - 1, emp_id, score))
    return out


def match_employees(text: str, directory: list[EmployeeRef],
                    edits: SurnameEdits) -> list[EmployeeMatch]:
    """Упоминания сотрудников по вариантам имён.

    Длинный вариант («Александр Смирнов») важнее одиночных слов; на одном спане
    остаются все сотрудники с равным лучшим баллом — неоднозначность не разрешаем.
    """
    found = list(WORD.finditer(text))
    cands = _candidates([m.group() for m in found], directory, edits)
    best: dict[tuple[int, int], tuple[float, set[int]]] = {}
    for c in cands:
        key = (c.first, c.last)
        score, ids = best.get(key, (0.0, set()))
        if c.score > score:
            best[key] = (c.score, {c.employee_id})
        elif c.score == score:
            ids.add(c.employee_id)
    order = sorted(best.items(), key=lambda kv: (-(kv[0][1] - kv[0][0]), -kv[1][0], kv[0][0]))
    taken: set[int] = set()
    out = []
    for (first, last), (_, ids) in order:
        if taken.intersection(range(first, last + 1)):
            continue
        taken.update(range(first, last + 1))
        start, end = found[first].start(), found[last].end()
        out.append(EmployeeMatch(Span(start, end, 'EMPLOYEE', text[start:end]), sorted(ids)))
    return sorted(out, key=lambda m: m.span.start)


Range = tuple[int, int]

# Падежные формы брендов: «Макита» → «Макиты», «Макитой»; «Кедр» → «Кедра», «Кедром».
# Окончаний фамилий (-ов, -ин, -ым) здесь нет: «Кедров» — не форма «кедр».
_BRAND_ENDINGS_A = ('а', 'ы', 'и', 'е', 'у', 'ой', 'ою', '', 'ам', 'ами', 'ах')
_BRAND_ENDINGS_CONS = ('', 'а', 'у', 'ом', 'ем', 'е', 'ы', 'и', 'ам', 'ами', 'ах')
_CYR_DECLINABLE = re.compile(r'[а-я]*[бвгджзклмнпрстфхцчшщ]|[а-я]+[ая]')


def _brand_forms(entry: str) -> set[str]:
    w = norm_name(entry)
    if not _CYR_DECLINABLE.fullmatch(w):  # латиница и несклоняемые — только как есть
        return {w}
    if w[-1] in 'ая':
        return {w[:-1] + e for e in _BRAND_ENDINGS_A}
    return {w + e for e in _BRAND_ENDINGS_CONS}


@lru_cache(maxsize=4)
def load_stoplist(path: Path = STOPLIST_PATH) -> frozenset[str]:
    """Формы брендов из стоп-листа (Д-21, Д-26): без регистра, с падежными окончаниями."""
    with open(path, 'rb') as f:
        return frozenset(f for w in tomllib.load(f)['words'] for f in _brand_forms(w))


@lru_cache(maxsize=100_000)
def _person_lemmas(word: str) -> frozenset[str]:
    return frozenset(norm_name(p.normal) for p in _morph().parse(word)
                     if any(g in p.tag for g in _PERSON_GRAMMEMES))


def stop_spans(text: str, stoplist: frozenset[str]) -> list[Range]:
    """Слова-бренды: сама форма в стоп-листе, и ни один разбор как имени не ведёт за его пределы.

    «Кедров» не бренд (форма вне списка), «Аврора» — бренд (имя «аврора» в списке).
    """
    return [(w.start(), w.end()) for w in WORD.finditer(text)
            if norm_name(w.group()) in stoplist and _person_lemmas(w.group()) <= stoplist]


def only_stop_words(text: str, start: int, end: int, stops: list[Range]) -> bool:
    """Спан распознавателя имён целиком из брендов: «Макита». «Аврора Петренко» — нет."""
    words = [(w.start(), w.end()) for w in WORD.finditer(text, start, end)]
    return bool(words) and set(words) <= set(stops)


def stop_neighbours(text: str, stops: list[Range], people: list[Range]) -> list[Range]:
    """Бренды вплотную (через пробелы) к имени или токену человека: «Бош Андрей» — имя целиком."""
    anchors, rest, out = list(people), [s for s in stops if s not in people], []
    changed = True
    while changed:
        changed = False
        for st in list(rest):
            if any(_adjacent(text, st, a) for a in anchors):
                rest.remove(st)
                out.append(st)
                anchors.append(st)
                changed = True
    return out


def _adjacent(text: str, a: Range, b: Range) -> bool:
    first, second = sorted([a, b])
    return first[1] <= second[0] and not text[first[1]:second[0]].strip()


# Латинское слово с заглавной без цифр рядом: «John», «Smith»; аббревиатуры до 5 букв — нет.
_LATIN = re.compile(r'(?<![\w\-])[A-Z][A-Za-z]*(?:-[A-Za-z]+)*(?![\w\-])')
_ABBR_MAX = 5


def latin_names(text: str, stoplist: frozenset[str]) -> list[Range]:
    """Имена латиницей — как внешние люди; бренды из стоп-листа и «GBH», «DCK» — нет."""
    return [(m.start(), m.end()) for m in _LATIN.finditer(text)
            if not (m.group().isupper() and len(m.group()) <= _ABBR_MAX)
            and norm_name(m.group()) not in stoplist]
