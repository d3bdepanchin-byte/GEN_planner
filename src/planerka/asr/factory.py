"""Выбор распознавателя по настройке."""
from planerka.asr.base import Transcriber
from planerka.asr.gigaam import GigaAMTranscriber
from planerka.asr.whisper import WhisperTranscriber
from planerka.settings import Env


def get_transcriber(engine: str, model: str, env: Env) -> Transcriber:
    if engine == 'gigaam':
        return GigaAMTranscriber(model, env.models_dir, env.hf_token)
    if engine == 'whisper':
        return WhisperTranscriber(model, env.models_dir)
    raise ValueError(f'неизвестный распознаватель: {engine}')
