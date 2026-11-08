"""Папка встречи на сервере и стенограмма в ней: data/meetings/<id>/transcript.json (R24)."""
import json
from dataclasses import asdict
from pathlib import Path

from planerka.asr.base import Segment, Transcript
from planerka.settings import Env

TRANSCRIPT_FILE = 'transcript.json'


def meeting_dir(env: Env, source_id: int) -> Path:
    return env.data_dir / 'meetings' / str(source_id)


def save_transcript(folder: Path, t: Transcript) -> Path:
    """Сегменты, движок, модель, длительность записи и время обработки."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / TRANSCRIPT_FILE
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(asdict(t), ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)  # без полузаписанного файла при сбое
    return path


def load_transcript(folder: Path) -> Transcript:
    data = json.loads((folder / TRANSCRIPT_FILE).read_text(encoding='utf-8'))
    return Transcript(segments=[Segment(**s) for s in data['segments']], engine=data['engine'],
                      model=data['model'], audio_sec=data['audio_sec'],
                      processing_sec=data['processing_sec'])
