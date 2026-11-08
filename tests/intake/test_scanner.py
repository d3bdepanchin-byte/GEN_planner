import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from planerka.db.models import Source
from planerka.intake.scanner import IntakeScanner, sha256_file

T0 = datetime(2026, 11, 2, 10, 0, 0)


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def scanner(inbox, data_dir):
    return IntakeScanner(inbox, data_dir)


def ready(scanner, session, base=T0):
    scanner.scan(session, base)
    return scanner.scan(session, base + timedelta(seconds=301))


def test_not_ready_until_stable(session, scanner, inbox):
    (inbox / "p.mp3").write_bytes(b"a" * 10)
    assert scanner.scan(session, T0) == []
    assert len(scanner.scan(session, T0 + timedelta(seconds=301))) == 1


def test_changing_file_resets_window(session, scanner, inbox):
    f = inbox / "p.mp3"
    f.write_bytes(b"a")
    scanner.scan(session, T0)
    f.write_bytes(b"ab")
    assert scanner.scan(session, T0 + timedelta(seconds=301)) == []
    assert len(scanner.scan(session, T0 + timedelta(seconds=602))) == 1


def test_duplicate_hash_ignored(session, scanner, inbox):
    (inbox / "a.mp3").write_bytes(b"same")
    (inbox / "b.mp3").write_bytes(b"same")
    scanner.scan(session, T0)
    assert len(scanner.scan(session, T0 + timedelta(seconds=301))) == 1


def test_duplicate_after_restart_ignored(session, scanner, inbox, data_dir):
    (inbox / "a.mp3").write_bytes(b"same")
    assert len(ready(scanner, session)) == 1
    restarted = IntakeScanner(inbox, data_dir)
    assert ready(restarted, session, T0 + timedelta(hours=1)) == []
    assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_stable_file_not_rehandled(session, scanner, inbox):
    (inbox / "a.mp3").write_bytes(b"x")
    assert len(ready(scanner, session)) == 1
    assert scanner.scan(session, T0 + timedelta(seconds=900)) == []


def test_original_kept_in_inbox(session, scanner, inbox, data_dir):
    (inbox / "p.mp3").write_bytes(b"x")
    scanner.scan(session, T0)
    src = scanner.scan(session, T0 + timedelta(seconds=301))[0]
    assert (inbox / "p.mp3").exists()
    copy = data_dir / "meetings" / str(src.id) / "original.mp3"
    assert copy.read_bytes() == b"x"
    assert src.path == str(copy)


def test_new_source_fields(session, scanner, inbox):
    f = inbox / "P.MP3"
    f.write_bytes(b"x")
    src = ready(scanner, session)[0]
    assert src.kind == "запись"
    assert src.status == "получен"
    assert src.received_at == T0 + timedelta(seconds=301)
    assert src.meeting_date == datetime.fromtimestamp(f.stat().st_mtime).date()
    assert src.flags["meeting_date_from"] == "mtime"
    assert src.sha256 == hashlib.sha256(b"x").hexdigest()
    assert src.path.endswith("original.MP3")


def test_file_changed_after_ready_is_requeued(session, scanner, inbox):
    f = inbox / "p.mp3"
    f.write_bytes(b"part1")
    scanner.scan(session, T0)
    first = scanner.scan(session, T0 + timedelta(seconds=301))[0]
    with f.open("ab") as h:
        h.write(b"part2")
    scanner.scan(session, T0 + timedelta(seconds=400))
    second = scanner.scan(session, T0 + timedelta(seconds=701))[0]
    assert second.id != first.id
    assert session.get(Source, first.id).status == "заменён"
    assert second.status == "получен"


def test_unsupported_and_sync_temp_skipped(session, scanner, inbox):
    (inbox / "notes.txt").write_bytes(b"x")
    (inbox / ".syncthing.p.mp3.tmp").write_bytes(b"x")
    (inbox / "~syncthing~q.mp3").write_bytes(b"x")
    scanner.scan(session, T0)
    assert scanner.scan(session, T0 + timedelta(seconds=301)) == []


def test_subfolders_scanned_hidden_dirs_skipped(session, scanner, inbox):
    (inbox / "laptop").mkdir()
    (inbox / "laptop" / "m.mp3").write_bytes(b"one")
    (inbox / ".stfolder").mkdir()
    (inbox / ".stfolder" / "x.mp3").write_bytes(b"two")
    (inbox / ".stversions").mkdir()
    (inbox / ".stversions" / "y.mp3").write_bytes(b"three")
    assert len(ready(scanner, session)) == 1


def test_copy_failure_leaves_no_source(session, scanner, inbox, monkeypatch):
    (inbox / "p.mp3").write_bytes(b"x")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("planerka.intake.scanner.shutil.copyfile", boom)
    assert ready(scanner, session) == []
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    monkeypatch.undo()
    assert scanner.scan(session, T0 + timedelta(seconds=400)) != []


def test_sha256_file_chunked(tmp_path):
    f = tmp_path / "f"
    data = b"z" * (3 * 1024 * 1024 + 5)
    f.write_bytes(data)
    assert sha256_file(f) == hashlib.sha256(data).hexdigest()
