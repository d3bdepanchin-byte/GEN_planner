"""Извлечение звука из записи: WAV 16 кГц, моно (вход для распознавания)."""
import subprocess
from pathlib import Path


def extract_audio(src: Path, dst_dir: Path) -> Path:
    """ffmpeg без shell; при ошибке — только код возврата (имя файла может содержать ПДн)."""
    dst = dst_dir / "audio.wav"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg завершился с кодом {result.returncode}")
    return dst
