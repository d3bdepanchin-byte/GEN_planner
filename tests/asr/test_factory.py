import pytest

from planerka.asr.base import TranscriberUnavailable
from planerka.asr.factory import get_transcriber
from planerka.asr.gigaam import GigaAMTranscriber
from planerka.asr.whisper import WhisperTranscriber


def test_factory_returns_gigaam(env):
    assert isinstance(get_transcriber('gigaam', 'v3_e2e_rnnt', env), GigaAMTranscriber)


def test_factory_returns_whisper(env):
    assert isinstance(get_transcriber('whisper', 'small', env), WhisperTranscriber)


def test_unknown_engine_raises(env):
    with pytest.raises(ValueError):
        get_transcriber('vosk', 'x', env)


@pytest.mark.parametrize('engine,model', [('gigaam', 'v3_e2e_rnnt'), ('whisper', 'small')])
def test_missing_library_is_unavailable(engine, model, env, tmp_path, monkeypatch):
    # без установленных библиотек или моделей: TranscriberUnavailable, имён файлов в тексте нет
    import sys
    monkeypatch.setitem(sys.modules, 'gigaam', None)
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    wav = tmp_path / 'secret_name.wav'
    wav.write_bytes(b'')
    with pytest.raises(TranscriberUnavailable) as e:
        get_transcriber(engine, model, env).transcribe(wav)
    assert 'secret_name' not in str(e.value)


def test_constructor_does_not_set_offline_flag(env, monkeypatch):
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    import os
    get_transcriber('gigaam', 'v3_e2e_rnnt', env)
    assert 'HF_HUB_OFFLINE' not in os.environ
