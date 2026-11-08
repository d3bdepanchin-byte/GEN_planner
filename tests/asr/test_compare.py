import pytest

from planerka.asr.base import Segment, Transcript
from planerka.asr.compare import (
    CompareResult,
    Decision,
    decide,
    decide_overall,
    fragment_text,
    names_recall,
    pick_whisper_cpu,
    render_report,
    wer,
)


def test_wer_ignores_case_yo_and_punctuation():
    assert wer('Ещё одна задача.', 'еще одна задача') == 0.0


def test_wer_counts_word_errors():
    assert wer('одна две три четыре', 'одна две три пять') == pytest.approx(0.25)


def test_wer_empty_reference_raises():
    with pytest.raises(ValueError):
        wer('  ...  ', 'что-то')


def test_names_recall_counts_repeats():
    assert names_recall(['Смирнов', 'Смирнов', 'Кузнецова'], 'смирнов сказал кузнецова') == pytest.approx(2 / 3)


def test_names_recall_repeats_matched_by_occurrences():
    assert names_recall(['Смирнов', 'Смирнов'], 'Смирнов и Смирнов') == 1.0


def test_names_recall_whole_words_only():
    assert names_recall(['Иванов'], 'Ивановский завод') == 0.0


def test_names_recall_brand_with_yo_and_case():
    assert names_recall(['Бош'], 'дрель БОШ') == 1.0
    assert names_recall(['Королёв'], 'королев пришёл') == 1.0


def test_names_recall_empty_list_raises():
    with pytest.raises(ValueError):
        names_recall([], 'текст')


def g(w, n, speed):
    return CompareResult('gigaam', 'v3_e2e_rnnt', w, n, speed)


W = CompareResult('whisper', 'medium', 0.20, 0.70, 1.0)


def test_gigaam_when_quality_and_speed_ok():
    assert decide(g(0.15, 0.80, 2.0), W, 14, 2) == Decision.GIGAAM


def test_whisper_when_gigaam_worse():
    assert decide(g(0.25, 0.80, 1.0), W, 14, 2) == Decision.WHISPER


def test_wait_for_gpu_when_quality_ok_but_slow():
    assert decide(g(0.15, 0.80, 8.0), W, 14, 2) == Decision.WAIT_FOR_GPU


def test_decide_boundaries_are_inclusive():
    assert decide(g(0.20, 0.70, 7.0), W, 14, 2) == Decision.GIGAAM


def test_whisper_when_names_worse_even_if_wer_better():
    assert decide(g(0.10, 0.60, 1.0), W, 14, 2) == Decision.WHISPER


def _t(*segs, **kw):
    return Transcript(segments=[Segment(*s) for s in segs], **kw)


def test_fragment_text_takes_overlapping_segments():
    t = _t((0, 10, 'раз'), (10, 20, 'два'), (20, 30, 'три'), (30, 40, 'четыре'))
    assert fragment_text(t, 15, 30) == 'два три'


def test_fragment_text_edges_touching_excluded():
    t = _t((0, 10, 'раз'), (10, 20, 'два'))
    assert fragment_text(t, 10, 20) == 'два'


def test_fragment_text_empty_when_nothing_overlaps():
    assert fragment_text(_t((0, 10, 'раз')), 50, 60) == ''


def test_fragment_text_bad_interval():
    with pytest.raises(ValueError):
        fragment_text(_t((0, 10, 'раз')), 60, 50)


def _w(model, speed):
    return CompareResult('whisper', model, 0.2, 0.7, speed)


def test_pick_whisper_cpu_largest_that_fits():
    res = [_w('large-v3', 9.0), _w('small', 1.0), _w('medium', 3.0)]
    picked, fits = pick_whisper_cpu(res, 14, 2)
    assert (picked.model, fits) == ('medium', True)


def test_pick_whisper_cpu_includes_large_when_it_fits():
    res = [_w('small', 1.0), _w('medium', 2.0), _w('large-v3', 6.0)]
    assert pick_whisper_cpu(res, 14, 2)[0].model == 'large-v3'


def test_pick_whisper_cpu_ignores_other_engines():
    res = [g(0.1, 0.9, 1.0), _w('small', 1.0)]
    assert pick_whisper_cpu(res, 14, 2)[0].engine == 'whisper'


def test_render_report_is_metrics_only():
    res = [_w('small', 1.0), CompareResult('gigaam', 'v3_e2e_rnnt', 0.15, 0.80, 2.0)]
    md = render_report('2026-11-08', res, Decision.GIGAAM, 14, 2,
                       whisper_cpu=res[0], gigaam=res[1], whisper_cpu_fits=True)
    assert md.startswith('# Сравнение моделей распознавания, 2026-11-08')
    assert 'полнота задач против протокола' in md
    assert 'gigaam' in md and 'v3_e2e_rnnt' in md and '0.150' in md
    assert 'Решение: GIGAAM' in md
    assert '14' in md and '2' in md


def _gv(model, w, n, speed):
    return CompareResult('gigaam', model, w, n, speed)


def test_overall_slow_best_wer_variant_does_not_hide_fast_passing_variant():
    res = [_w('medium', 1.0), _gv('v3_e2e_rnnt', 0.15, 0.80, 8.0), _gv('v3_e2e_ctc', 0.16, 0.80, 1.0)]
    res[0] = CompareResult('whisper', 'medium', 0.20, 0.70, 1.0)
    d, giga, _, _ = decide_overall(res, 14, 2)
    assert (d, giga.model) == (Decision.GIGAAM, 'v3_e2e_ctc')


def test_overall_lowest_wer_variant_failing_names_does_not_hide_passing_one():
    res = [CompareResult('whisper', 'medium', 0.20, 0.70, 1.0),
           _gv('v3_e2e_rnnt', 0.10, 0.60, 1.0), _gv('v3_e2e_ctc', 0.15, 0.80, 1.0)]
    d, giga, _, _ = decide_overall(res, 14, 2)
    assert (d, giga.model) == (Decision.GIGAAM, 'v3_e2e_ctc')


def test_overall_tie_broken_by_wer_then_names_then_speed():
    res = [CompareResult('whisper', 'medium', 0.20, 0.70, 1.0),
           _gv('v3_e2e_rnnt', 0.15, 0.80, 2.0), _gv('v3_e2e_ctc', 0.12, 0.75, 3.0)]
    assert decide_overall(res, 14, 2)[1].model == 'v3_e2e_ctc'


def test_overall_nothing_fits_uses_largest_whisper_and_waits_for_gpu():
    res = [_w('small', 9.0), _w('medium', 10.0), _gv('v3_e2e_rnnt', 0.30, 0.10, 1.0)]
    d, _, base, fits = decide_overall(res, 14, 2)
    assert (base.model, fits, d) == ('medium', False, Decision.WAIT_FOR_GPU)


def test_pick_whisper_cpu_none_fits_is_largest_flagged():
    picked, fits = pick_whisper_cpu([_w('medium', 9.0), _w('small', 8.0)], 14, 2)
    assert (picked.model, fits) == ('medium', False)


def test_render_report_states_nothing_fits_in_decision_line():
    res = [_w('small', 9.0), CompareResult('gigaam', 'v3_e2e_rnnt', 0.15, 0.80, 2.0)]
    md = render_report('2026-11-08', res, Decision.WAIT_FOR_GPU, 14, 2,
                       whisper_cpu=res[0], gigaam=res[1], whisper_cpu_fits=False)
    line = next(l for l in md.splitlines() if l.startswith('Решение:'))
    assert 'WAIT_FOR_GPU' in line and 'ни одна модель whisper не укладывается в окно' in line.lower()
    assert 'Берём Whisper' not in md


def test_tool_runs_end_to_end_with_fake_transcribers(tmp_path, env):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location('asr_compare', Path('tools/asr_compare.py'))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    ref = tmp_path / 'ref.txt'
    ref.write_text('Смирнов принёс дрель Бош', encoding='utf-8')
    names = tmp_path / 'names.txt'
    names.write_text('Смирнов\nБош\n', encoding='utf-8')

    def make(engine, model, _env):
        class T:
            def transcribe(self, wav):
                return Transcript([Segment(0, 5, 'вступление'), Segment(5, 20, 'Смирнов принёс дрель Бош')],
                                  engine, model, audio_sec=3600, processing_sec=1800)
        return T()

    args = tool.parse_args(['--recording', 'a.wav', '--reference', str(ref), '--start', '5', '--end', '20',
                            '--names', str(names), '--out-dir', str(tmp_path / 'reports')])
    path = tool.run(args, env, make)
    md = path.read_text(encoding='utf-8')
    assert path.name.startswith('asr_compare_') and path.suffix == '.md'
    assert 'v3_e2e_ctc' in md and 'large-v3' in md and 'Решение: GIGAAM' in md
    assert 'принёс' not in md and 'вступление' not in md
