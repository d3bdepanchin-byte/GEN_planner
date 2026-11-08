"""Отображение результата распознавания в Transcript — на фейковых gigaam / faster_whisper."""
import os
import sys
import types
import wave
from types import SimpleNamespace as NS

import pytest

from planerka.asr.base import NoSpeech, TranscriberUnavailable, TranscriptionFailed
from planerka.asr.factory import get_transcriber


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / 'rec_secret.wav'
    with wave.open(str(p), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 16000 * 3)  # 3 секунды
    return p


def _fake_gigaam(monkeypatch, segments=None, load_error=None, run_error=None):
    class Model:
        def transcribe_longform(self, path):
            if run_error:
                raise run_error
            return NS(segments=segments)

    def load_model(name, download_root=None, **kw):
        if load_error:
            raise load_error
        return Model()

    monkeypatch.setitem(sys.modules, 'gigaam', types.SimpleNamespace(load_model=load_model))


def _fake_whisper(monkeypatch, segments=None, duration=3.0, load_error=None, run_error=None):
    class WhisperModel:
        def __init__(self, *a, **kw):
            if load_error:
                raise load_error

        def transcribe(self, path, language=None):
            if run_error:
                raise run_error
            return iter(segments), NS(duration=duration)

    monkeypatch.setitem(
        sys.modules, 'faster_whisper', types.SimpleNamespace(WhisperModel=WhisperModel)
    )


def _seg(start, end, text):
    return NS(start=start, end=end, text=text)


def test_gigaam_strips_drops_empty_sorts_and_fills_fields(env, wav, monkeypatch):
    _fake_gigaam(monkeypatch, [_seg(5.0, 6.0, ' второй '), _seg(2.0, 2.5, '   '), _seg(0.5, 1.0, 'первый')])
    t = get_transcriber('gigaam', 'v3_e2e_rnnt', env).transcribe(wav)
    assert [(s.start, s.text) for s in t.segments] == [(0.5, 'первый'), (5.0, 'второй')]
    assert (t.engine, t.model) == ('gigaam', 'v3_e2e_rnnt')
    assert t.audio_sec == pytest.approx(3.0)
    assert t.processing_sec > 0


def test_gigaam_audio_sec_falls_back_to_last_segment_end(env, tmp_path, monkeypatch):
    bad = tmp_path / 'x.wav'
    bad.write_bytes(b'not a wav')
    _fake_gigaam(monkeypatch, [_seg(0.0, 4.0, 'а'), _seg(4.0, 9.5, 'б')])
    t = get_transcriber('gigaam', 'v3_e2e_rnnt', env).transcribe(bad)
    assert t.audio_sec == 9.5


def test_whisper_maps_segments_and_duration(env, wav, monkeypatch):
    _fake_whisper(monkeypatch, [_seg(1.0, 2.0, ' да '), _seg(2.0, 3.0, ''), _seg(3.0, 4.0, 'нет')], duration=12.5)
    t = get_transcriber('whisper', 'small', env).transcribe(wav)
    assert [s.text for s in t.segments] == ['да', 'нет']
    assert (t.engine, t.model, t.audio_sec) == ('whisper', 'small', 12.5)
    assert t.processing_sec > 0


@pytest.mark.parametrize('engine,fake', [('gigaam', _fake_gigaam), ('whisper', _fake_whisper)])
def test_no_segments_is_no_speech(engine, fake, env, wav, monkeypatch):
    fake(monkeypatch, [_seg(0.0, 1.0, '  ')])
    with pytest.raises(NoSpeech):
        get_transcriber(engine, 'm', env).transcribe(wav)


@pytest.mark.parametrize('engine,fake', [('gigaam', _fake_gigaam), ('whisper', _fake_whisper)])
def test_load_failure_is_unavailable_not_failed(engine, fake, env, wav, monkeypatch):
    fake(monkeypatch, [], load_error=RuntimeError('boom secret_text'))
    with pytest.raises(TranscriberUnavailable) as e:
        get_transcriber(engine, 'm', env).transcribe(wav)
    assert not isinstance(e.value, TranscriptionFailed)
    assert 'RuntimeError' in str(e.value) and 'secret' not in str(e.value)


@pytest.mark.parametrize('engine,fake', [('gigaam', _fake_gigaam), ('whisper', _fake_whisper)])
def test_recognition_error_is_transcription_failed(engine, fake, env, wav, monkeypatch):
    fake(monkeypatch, [], run_error=ValueError('corrupt secret_text'))
    with pytest.raises(TranscriptionFailed) as e:
        get_transcriber(engine, 'm', env).transcribe(wav)
    assert isinstance(e.value, TranscriberUnavailable)
    assert 'ValueError' in str(e.value)
    assert 'secret' not in str(e.value) and 'rec_' not in str(e.value)


@pytest.mark.parametrize('engine,fake', [('gigaam', _fake_gigaam), ('whisper', _fake_whisper)])
@pytest.mark.parametrize('fail', [False, True])
def test_environ_unchanged(engine, fake, fail, env, wav, monkeypatch):
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    monkeypatch.delenv('HF_TOKEN', raising=False)
    before = dict(os.environ)
    fake(monkeypatch, [_seg(0.0, 1.0, 'а')], load_error=RuntimeError('x') if fail else None)
    try:
        get_transcriber(engine, 'm', env).transcribe(wav)
    except TranscriberUnavailable:
        assert fail
    assert dict(os.environ) == before
