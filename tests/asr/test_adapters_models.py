import pytest

from planerka.asr.factory import get_transcriber


@pytest.mark.models
@pytest.mark.parametrize('engine,model', [('gigaam', 'v3_e2e_rnnt'), ('whisper', 'small')])
def test_segments_monotonic_offline(engine, model, env, sample_wav, no_network):
    t = get_transcriber(engine, model, env).transcribe(sample_wav)
    starts = [s.start for s in t.segments]
    assert t.segments and starts == sorted(starts)
    assert t.processing_sec > 0
