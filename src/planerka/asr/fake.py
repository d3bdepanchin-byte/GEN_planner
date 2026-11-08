"""Фейковый распознаватель для тестов."""
from pathlib import Path

from planerka.asr.base import Transcript, TranscriberUnavailable


class FakeTranscriber:
    def __init__(self, by_name: dict[str, Transcript]):
        self.by_name = by_name

    def transcribe(self, wav: Path) -> Transcript:
        try:
            return self.by_name[wav.name]
        except KeyError:
            raise TranscriberUnavailable('no transcript for this file') from None
