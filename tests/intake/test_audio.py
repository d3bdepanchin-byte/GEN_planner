import subprocess
import wave

import pytest

from planerka.intake.audio import extract_audio


def test_extract_audio_16k_mono(tmp_path):
    src = tmp_path / "s.mp3"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(src)],
        check=True, capture_output=True)
    dst = tmp_path / "out"
    dst.mkdir()
    out = extract_audio(src, dst)
    assert out == dst / "audio.wav"
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)


def test_extract_audio_failure_reports_code_only(tmp_path):
    src = tmp_path / "Иванов секрет.mp3"
    src.write_bytes(b"not audio")
    with pytest.raises(RuntimeError) as exc:
        extract_audio(src, tmp_path)
    assert "Иванов" not in str(exc.value)
    assert "код" in str(exc.value)
