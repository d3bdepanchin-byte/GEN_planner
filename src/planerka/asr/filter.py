"""Определение типа встречи по фразе открытия в начале записи."""
import re
from enum import Enum

from planerka.asr.base import NoSpeech, Transcript


class MeetingKind(str, Enum):
    PLANERKA = 'planerka'
    ONE_ON_ONE = 'one_on_one'


def normalize(text: str) -> str:
    text = text.lower().replace('ё', 'е')
    text = re.sub(r'[^\w\s]|_', ' ', text)
    return ' '.join(text.split())


def detect_meeting_kind(
    t: Transcript, phrases: dict[str, list[str]], window_sec: int
) -> MeetingKind | None:
    if not any(s.text.strip() for s in t.segments):
        raise NoSpeech('empty transcript')
    head = normalize(' '.join(s.text for s in t.segments if s.start <= window_sec))
    haystack = f' {head} '
    best: tuple[int, MeetingKind] | None = None
    for kind in MeetingKind:
        for phrase in phrases.get(kind.value, []):
            p = normalize(phrase)
            if not p:
                continue
            pos = haystack.find(f' {p} ')
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, kind)
    return best[1] if best else None
