import json

import pytest
from sqlalchemy import func, select

from planerka.asr.fake import FakeTranscriber
from planerka.db.models import Source, Task, TaskEdit
from planerka.masking.checker import MaskingUncertain
from planerka.pipeline.reprocess import reprocess
from samples import EXPECTED_CANDIDATES, NO_PHRASE, PassThroughMasker


def test_reprocess_creates_no_tasks(session, deps, processed_source):
    before = session.scalar(select(func.count(Task.id)))
    reprocess(session, processed_source.id, deps)
    assert session.scalar(select(func.count(Task.id))) == before
    assert session.scalar(select(func.count(TaskEdit.id))) == 0


def test_reprocess_writes_transcript_result_and_comparison(session, deps, processed_source, env):
    status = session.get(Source, processed_source.id).status
    out = reprocess(session, processed_source.id, deps)
    assert out.parent == env.data_dir / 'meetings' / str(processed_source.id)
    assert out.name.startswith('reprocess_')
    transcript = json.loads((out / 'transcript.json').read_text(encoding='utf-8'))
    assert transcript['engine'] == 'fake' and transcript['segments']
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    assert len(result['candidates']) == EXPECTED_CANDIDATES
    assert result['candidates'][0]['owner'] == 'Смирнов Александр'
    report = (out / 'comparison.md').read_text(encoding='utf-8')
    for t in session.scalars(select(Task).where(Task.source_id == processed_source.id)):
        assert f'И-{t.number:03d}' in report
    assert not (out / 'audio.wav').exists()
    assert session.get(Source, processed_source.id).status == status  # источник не трогаем


def test_reprocess_respects_masking(session, deps, processed_source, directory):
    deps.masker = PassThroughMasker(directory)
    asked = len(deps.llm.requests)
    with pytest.raises(MaskingUncertain):
        reprocess(session, processed_source.id, deps)
    assert len(deps.llm.requests) == asked


def test_reprocess_unknown_source(session, deps):
    with pytest.raises(ValueError):
        reprocess(session, 10**9, deps)


def test_reprocess_without_opening_phrase_skips_llm(session, deps, processed_source):
    deps.transcriber = FakeTranscriber({'audio.wav': NO_PHRASE})
    asked = len(deps.llm.requests)
    out = reprocess(session, processed_source.id, deps)
    assert len(deps.llm.requests) == asked
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    assert result['candidates'] == [] and result['meeting_kind'] == 'нет фразы открытия'
