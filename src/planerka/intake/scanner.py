"""Приём записей из каталога Syncthing (Д-51, Д-69).

Файл готов, когда размер и mtime не менялись intake.stability_window_sec.
Наблюдения живут в памяти: после перезапуска окно отсчитывается заново,
а дубликаты отсекаются по sha256 в БД. В логах — только номера источников.
"""
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from planerka.db.models import Source
from planerka.settings import get_setting

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _is_sync_temp(name: str) -> bool:
    return (name.startswith(".syncthing.") and name.endswith(".tmp")) or name.startswith("~syncthing~")


class _Duplicate(Exception):
    pass


@dataclass
class _Observation:
    size: int
    mtime_ns: int
    stable_since: datetime
    handled: bool = False


class IntakeScanner:
    def __init__(self, inbox: Path, data_dir: Path):
        self.inbox = inbox
        self.data_dir = data_dir
        self._seen: dict[Path, _Observation] = {}

    def _candidates(self, extensions: set[str]) -> list[Path]:
        found: list[Path] = []
        for root, dirs, files in os.walk(self.inbox):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if _is_sync_temp(name) or Path(name).suffix.lower() not in extensions:
                    continue
                found.append(Path(root) / name)
        return found

    def scan(self, session: Session, now: datetime) -> list[Source]:
        window = get_setting(session, "intake.stability_window_sec")
        extensions = {e.lower() for e in get_setting(session, "intake.extensions")}
        created: list[Source] = []
        paths = self._candidates(extensions)
        present = set(paths)
        for gone in set(self._seen) - present:
            del self._seen[gone]
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                continue
            obs = self._seen.get(path)
            if obs is None or (obs.size, obs.mtime_ns) != (st.st_size, st.st_mtime_ns):
                self._seen[path] = _Observation(st.st_size, st.st_mtime_ns, now)
                continue
            if obs.handled or (now - obs.stable_since).total_seconds() < window:
                continue
            source, handled = self._take(session, path, st.st_mtime, now)
            if source is not None:
                created.append(source)
            obs.handled = handled
        return created

    def _take(
        self, session: Session, path: Path, mtime: float, now: datetime
    ) -> tuple[Source | None, bool]:
        """(новый источник или None, обработан ли файл). Сбой копирования — не обработан, повтор."""
        key = hashlib.sha256(path.relative_to(self.inbox).as_posix().encode()).hexdigest()
        source = Source(
            kind="запись", path="", sha256=f"pending-{uuid.uuid4().hex}", received_at=now,
            status="получен", meeting_date=datetime.fromtimestamp(mtime).date(),
            flags={"meeting_date_from": "mtime", "inbox_key": key})
        target_dir: Path | None = None
        try:
            with session.begin_nested():
                session.add(source)
                session.flush()
                target_dir = self.data_dir / "meetings" / str(source.id)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"original{path.suffix}"
                shutil.copyfile(path, target)
                digest = sha256_file(target)  # хеш именно сохранённой копии
                if session.scalar(select(Source.id).where(Source.sha256 == digest)) is not None:
                    raise _Duplicate
                source.sha256 = digest
                source.path = str(target)
        except _Duplicate:
            shutil.rmtree(target_dir, ignore_errors=True)
            return None, True
        except OSError as exc:
            log.warning("Приём записи не удался: %s", type(exc).__name__)
            if target_dir is not None:
                shutil.rmtree(target_dir, ignore_errors=True)
            return None, False
        except BaseException:
            if target_dir is not None:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise
        session.flush()
        older = session.scalars(select(Source).where(
            Source.flags["inbox_key"].as_string() == key,
            Source.id != source.id, Source.status != "заменён")).all()
        for old in older:
            old.status = "заменён"
            log.info("Источник %s заменён источником %s", old.id, source.id)
        session.flush()
        log.info("Принят источник %s", source.id)
        return source, True
