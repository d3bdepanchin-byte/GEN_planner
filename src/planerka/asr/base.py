"""Интерфейс распознавания речи: данные и протокол Transcriber."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class TranscriberUnavailable(Exception):
    """Распознаватель недоступен или не знает такой записи. Без текста стенограммы."""


class NoSpeech(Exception):
    """В записи нет речи. Без текста стенограммы."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _timecode(sec: float) -> str:
    total = int(sec)
    return f'[{total // 60:02d}:{total % 60:02d}]'


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)
    engine: str = ''
    model: str = ''
    audio_sec: float = 0.0
    processing_sec: float = 0.0

    @property
    def text(self) -> str:
        return '\n'.join(f'{_timecode(s.start)} {s.text}' for s in self.segments)


class Transcriber(Protocol):
    def transcribe(self, wav: Path) -> Transcript: ...
