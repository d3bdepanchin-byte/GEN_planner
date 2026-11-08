"""Адаптер GigaAM (salute-developers/GigaAM). Библиотека импортируется при загрузке модели."""
import time
from pathlib import Path

from planerka.asr._hub import hub_env, wav_duration
from planerka.asr.base import (
    NoSpeech,
    Segment,
    Transcript,
    TranscriberUnavailable,
    TranscriptionFailed,
)


class GigaAMTranscriber:
    """Сверено с README и исходниками (08.11.2026):
    - gigaam.load_model(model_name, fp16_encoder=True, use_flash=False, device=None,
      download_root=None) — download_root по умолчанию ~/.cache/gigaam;
    - model.transcribe_longform(wav_file, word_timestamps=False, ...) возвращает
      LongformTranscriptionResult со списком segments, у каждого start, end, text;
    - pyannote/segmentation-3.0: токен HF с read-доступом и принятые условия; README
      передаёт токен переменной окружения HF_TOKEN.
    Не проверено: единицы start/end (README не говорит; ожидаются секунды), куда pyannote
    кладёт кэш под HF_HUB_OFFLINE=1, есть ли регистр в выходе v3_e2e_* (README: «пунктуация
    и нормализация текста», про регистр молчит). Всё — на серверном прогоне.
    """

    engine = 'gigaam'

    def __init__(self, model: str, models_dir: Path, hf_token: str):
        self.model = model
        self.models_dir = models_dir
        self.hf_token = hf_token
        self._loaded = None

    def _load(self):
        if self._loaded is None:
            try:
                with hub_env(HF_TOKEN=self.hf_token):
                    import gigaam  # noqa: PLC0415 — тяжёлая, ставится не везде

                    self._loaded = gigaam.load_model(
                        self.model, download_root=str(self.models_dir / 'gigaam')
                    )
            except Exception as e:  # библиотека не стоит, модели нет, сбой загрузки
                raise TranscriberUnavailable(
                    f'GigaAM {self.model} не загрузилась: {type(e).__name__}'
                ) from None
        return self._loaded

    def transcribe(self, wav: Path) -> Transcript:
        model = self._load()
        started = time.monotonic()
        try:
            with hub_env(HF_TOKEN=self.hf_token):
                result = model.transcribe_longform(str(wav))
                segments = [
                    Segment(float(s.start), float(s.end), s.text.strip())
                    for s in result.segments
                    if s.text and s.text.strip()
                ]
        except Exception as e:
            raise TranscriptionFailed(
                f'GigaAM не распознал запись: {type(e).__name__}'
            ) from None
        processing = time.monotonic() - started
        if not segments:
            raise NoSpeech('в записи нет речи')
        segments.sort(key=lambda s: s.start)
        audio = wav_duration(wav)
        return Transcript(
            segments=segments,
            engine=self.engine,
            model=self.model,
            audio_sec=audio if audio is not None else segments[-1].end,
            processing_sec=processing,
        )
