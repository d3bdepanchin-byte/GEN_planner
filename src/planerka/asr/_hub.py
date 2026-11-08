"""Общее для адаптеров: офлайн-режим Hugging Face на время загрузки и распознавания."""
import os
import wave
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def hub_env(**extra: str):
    """Выставляет HF_HUB_OFFLINE=1 (модели скачаны заранее в models_dir, Д-25) и extra
    только на время блока, потом возвращает прежние значения — в процесс тестов не течёт.
    Воркер дополнительно задаёт HF_HUB_OFFLINE=1 в своём окружении: huggingface_hub читает
    флаг при импорте, и если он уже импортирован, поздняя установка не действует
    (не проверено, поэтому окружение воркера — основной способ)."""
    values = {'HF_HUB_OFFLINE': '1', **{k: v for k, v in extra.items() if v}}
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def wav_duration(wav: Path) -> float | None:
    """Длительность PCM-wav, секунд; None, если файл не читается стандартным модулем wave."""
    try:
        with wave.open(str(wav), 'rb') as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return None
