import pytest

from planerka.asr.base import (
    NoSpeech,
    Segment,
    Transcript,
    TranscriberUnavailable,
)
from planerka.asr.fake import FakeTranscriber
from planerka.asr.filter import MeetingKind, detect_meeting_kind, normalize
from pathlib import Path

PHRASES = {'planerka': ['начинаем планёрку'], 'one_on_one': ['встреча один на один']}


def tr(*segs):
    return Transcript([Segment(s, s + 5, x) for s, x in segs], 'fake', 'fake', 600, 1)


def test_planerka_phrase_in_window_detected():
    assert detect_meeting_kind(tr((12, 'Так. Начинаем планерку!')), PHRASES, 180) == MeetingKind.PLANERKA


def test_phrase_after_window_ignored():
    t = tr((5, 'всем привет'), (400, 'начинаем планёрку'))
    assert detect_meeting_kind(t, PHRASES, 180) is None


def test_one_on_one_detected():
    assert detect_meeting_kind(tr((3, 'Встреча один на один.')), PHRASES, 180) == MeetingKind.ONE_ON_ONE


def test_empty_transcript_raises_no_speech():
    with pytest.raises(NoSpeech):
        detect_meeting_kind(tr(), PHRASES, 180)


def test_whitespace_only_raises_no_speech():
    with pytest.raises(NoSpeech):
        detect_meeting_kind(tr((1, '  '), (2, '\n')), PHRASES, 180)


def test_text_has_timecodes():
    assert tr((65, 'итоги')).text == '[01:05] итоги'


def test_text_minutes_over_59_and_lines():
    assert tr((4510, 'а'), (5, 'б')).text == '[75:10] а\n[00:05] б'


def test_normalize():
    assert normalize('  Ёлка, ПЛАНЁРКА!  кто-то  ') == 'елка планерка кто то'


def test_phrase_split_across_segments():
    t = tr((10, 'ну начинаем'), (15, 'планерку коллеги'))
    assert detect_meeting_kind(t, PHRASES, 180) == MeetingKind.PLANERKA


def test_whole_word_only():
    t = tr((10, 'мы не начинаем планерку' + 'х'))
    assert detect_meeting_kind(t, PHRASES, 180) is None


def test_earliest_phrase_wins():
    t = tr((5, 'встреча один на один'), (20, 'начинаем планерку'))
    assert detect_meeting_kind(t, PHRASES, 180) == MeetingKind.ONE_ON_ONE


def test_segment_at_window_boundary_included():
    assert detect_meeting_kind(tr((180, 'начинаем планерку')), PHRASES, 180) == MeetingKind.PLANERKA


def test_fake_transcriber():
    t = tr((1, 'x'))
    f = FakeTranscriber({'a.wav': t})
    assert f.transcribe(Path('/x/a.wav')) is t
    with pytest.raises(TranscriberUnavailable):
        f.transcribe(Path('/x/b.wav'))
