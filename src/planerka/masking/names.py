"""Имена: леммы natasha, распознаватель имён (NER) и сопоставление со справочником (Д-16, 17)."""
import re
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from rapidfuzz import fuzz

from planerka.core.directory import EmployeeRef, norm_name
from planerka.masking.patterns import Span

WORD = re.compile(r'[^\W\d_]+(?:-[^\W\d_]+)*')


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


def _word_score(word: str, vword: str, is_surname: bool, threshold: int) -> float:
    a, b = lemmas(word), lemmas(vword)
    if a & b:
        return 100.0
    if not is_surname:  # имена и уменьшительные — только точная лемма
        return 0.0
    best = max(fuzz.ratio(x, y) for x in a for y in b)
    return best if best >= threshold else 0.0


def _variant_keys(emp: EmployeeRef) -> set[tuple[_VKey, ...]]:
    parts = emp.full_name.split()
    surname = norm_name(parts[0]) if parts else ''
    return {tuple((w, norm_name(w) == surname) for w in v.split())
            for v in emp.variants if v.split()}


def _candidates(words: list[str], directory: list[EmployeeRef],
                threshold: int) -> list[_Candidate]:
    variants = {emp.id: _variant_keys(emp) for emp in directory}
    vkeys = {k for vs in variants.values() for v in vs for k in v}
    # Каждое различное слово текста сравниваем с каждым словом вариантов один раз.
    scores: dict[str, dict[_VKey, float]] = {}
    positions: dict[_VKey, list[int]] = {}
    for i, word in enumerate(words):
        if word not in scores:
            scores[word] = {k: sc for k in vkeys
                            if (sc := _word_score(word, k[0], k[1], threshold))}
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
                    threshold: int) -> list[EmployeeMatch]:
    """Упоминания сотрудников по вариантам имён.

    Длинный вариант («Александр Смирнов») важнее одиночных слов; на одном спане
    остаются все сотрудники с равным лучшим баллом — неоднозначность не разрешаем.
    """
    found = list(WORD.finditer(text))
    cands = _candidates([m.group() for m in found], directory, threshold)
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
