"""Сравнение моделей распознавания (Д-24, Д-82). Запускается на сервере.

Записи, вычитанный фрагмент и список имён на сервере; в репозиторий они не попадают.

    python tools/asr_compare.py \\
        --recording /srv/planerka/data/planerka_05.11.wav \\
        --recording /srv/planerka/data/planerka_06.11.wav \\
        --reference /srv/planerka/data/fragment_ref.txt --start 600 --end 1000 \\
        --names /srv/planerka/data/names.txt

Что делает: гоняет Whisper small, medium, large-v3 (замер скорости), затем GigaAM
v3_e2e_rnnt и v3_e2e_ctc. WER и полнота имён считаются по сегментам, пересекающим
интервал --start..--end (секунды) в записи с номером --fragment-recording (по умолчанию
первая). Скорость считается по всем записям. База whisper_cpu — самая крупная модель
Whisper, которая укладывается в окно; large-v3 идёт в отчёт для справки.
Отчёт: reports/asr_compare_<дата>.md (вне git), только метрики, без текста записей.
Колонку «полнота задач против протокола» заполняет владелец реестра после задачи 15.
Итог — запись в журнал решений и настройки asr.engine, asr.model.
"""
import argparse
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from planerka.asr.base import Transcript, Transcriber
from planerka.asr.compare import (
    WHISPER_SIZES,
    CompareResult,
    decide,
    fragment_text,
    names_recall,
    pick_gigaam,
    pick_whisper_cpu,
    render_report,
    wer,
)
from planerka.asr.factory import get_transcriber
from planerka.settings import Env, load_defaults, load_env

CANDIDATES = [('whisper', m) for m in WHISPER_SIZES] + [
    ('gigaam', 'v3_e2e_rnnt'),
    ('gigaam', 'v3_e2e_ctc'),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--recording', type=Path, action='append', required=True,
                   help='путь к полной записи; можно повторять (05.11, 06.11)')
    p.add_argument('--reference', type=Path, required=True, help='файл с вычитанным вручную фрагментом')
    p.add_argument('--start', type=float, required=True, help='начало фрагмента в записи, с')
    p.add_argument('--end', type=float, required=True, help='конец фрагмента в записи, с')
    p.add_argument('--names', type=Path, required=True, help='фамилии и бренды фрагмента, по одному в строке')
    p.add_argument('--fragment-recording', type=int, default=0,
                   help='номер записи с фрагментом, с нуля (по умолчанию 0)')
    p.add_argument('--window-hours', type=float, help='окно обработки, ч (по умолчанию asr.overnight_window_hours)')
    p.add_argument('--meeting-hours', type=float, help='длина встречи, ч (по умолчанию asr.meeting_hours_max)')
    p.add_argument('--out-dir', type=Path, default=Path('reports'))
    return p.parse_args(argv)


def measure(
    engine: str, model: str, transcriber: Transcriber, recordings: list[Path],
    fragment_idx: int, reference: str, names: list[str], start: float, end: float,
) -> CompareResult:
    transcripts: list[Transcript] = [transcriber.transcribe(r) for r in recordings]
    audio = sum(t.audio_sec for t in transcripts)
    if audio <= 0:
        raise ValueError('нулевая длительность аудио')
    hypothesis = fragment_text(transcripts[fragment_idx], start, end)
    return CompareResult(
        engine, model,
        wer(reference, hypothesis),
        names_recall(names, hypothesis),
        sum(t.processing_sec for t in transcripts) / audio,
    )


def run(
    args: argparse.Namespace, env: Env,
    make: Callable[[str, str, Env], Transcriber] = get_transcriber,
) -> Path:
    defaults = load_defaults()
    window = args.window_hours if args.window_hours is not None else defaults['asr.overnight_window_hours']
    meeting = args.meeting_hours if args.meeting_hours is not None else defaults['asr.meeting_hours_max']
    reference = args.reference.read_text(encoding='utf-8')
    names = [n.strip() for n in args.names.read_text(encoding='utf-8').splitlines() if n.strip()]

    results: list[CompareResult] = []
    for engine, model in CANDIDATES:
        r = measure(engine, model, make(engine, model, env), args.recording,
                    args.fragment_recording, reference, names, args.start, args.end)
        print(f'{engine} {model}: wer={r.wer:.3f} names={r.names_recall:.3f} '
              f'speed={r.hours_per_audio_hour:.2f}', file=sys.stderr)
        results.append(r)

    whisper_cpu, fits = pick_whisper_cpu(results, window, meeting)
    gigaam = pick_gigaam(results)
    decision = decide(gigaam, whisper_cpu, window, meeting)
    today = date.today().isoformat()
    report = render_report(today, results, decision, window, meeting,
                           whisper_cpu=whisper_cpu, gigaam=gigaam, whisper_cpu_fits=fits)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f'asr_compare_{today}.md'
    path.write_text(report, encoding='utf-8')
    return path


def main(argv: list[str] | None = None) -> int:
    path = run(parse_args(argv), load_env())
    print(f'отчёт: {path}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
