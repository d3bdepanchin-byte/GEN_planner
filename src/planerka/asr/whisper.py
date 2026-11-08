"""Адаптер Whisper на faster-whisper. Библиотека импортируется при загрузке модели."""
import time
from pathlib import Path

from planerka.asr._hub import hub_env
from planerka.asr.base import NoSpeech, Segment, Transcript, TranscriberUnavailable


class WhisperTranscriber:
    """Сверено с README/исходниками faster-whisper (08.11.2026):
    WhisperModel(model_size_or_path, device='auto', compute_type='default',
    download_root=None, local_files_only=False, ...);
    model.transcribe(audio, language=None, ...) -> (генератор сегментов, info);
    у сегмента start, end, text; у info language, duration (секунды).
    """

    engine = 'whisper'

    def __init__(self, model: str, models_dir: Path, compute_type: str = 'int8'):
        self.model = model
        self.models_dir = models_dir
        self.compute_type = compute_type
        self._loaded = None

    def _load(self):
        if self._loaded is None:
            try:
                with hub_env():
                    from faster_whisper import WhisperModel  # noqa: PLC0415

                    self._loaded = WhisperModel(
                        self.model,
                        device='cpu',
                        compute_type=self.compute_type,
                        download_root=str(self.models_dir / 'whisper'),
                        local_files_only=True,
                    )
            except Exception as e:
                raise TranscriberUnavailable(
                    f'Whisper {self.model} не загрузился: {type(e).__name__}'
                ) from None
        return self._loaded

    def transcribe(self, wav: Path) -> Transcript:
        model = self._load()
        started = time.monotonic()
        try:
            with hub_env():
                raw, info = model.transcribe(str(wav), language='ru')
                segments = [
                    Segment(float(s.start), float(s.end), s.text.strip())
                    for s in raw  # генератор: распознавание идёт здесь
                    if s.text.strip()
                ]
        except Exception as e:
            raise TranscriberUnavailable(
                f'Whisper не распознал запись: {type(e).__name__}'
            ) from None
        processing = time.monotonic() - started
        if not segments:
            raise NoSpeech('в записи нет речи')
        return Transcript(
            segments=segments,
            engine=self.engine,
            model=self.model,
            audio_sec=float(info.duration),
            processing_sec=processing,
        )
