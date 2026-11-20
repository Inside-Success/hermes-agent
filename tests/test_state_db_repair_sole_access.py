"""Automatic state.db repair runs only with sole access to the database.

One profile state.db is open in several processes at once: the gateway, the
dashboard, and every agent / cron child. Repairing it while any of them has it
open, or closing a raw file descriptor on it inside a process that holds SQLite
locks, corrupts it. These tests hold the database open from another connection
or another process and check that every automatic repair path (open-time
``repair_state_db_schema``, the runtime in-place FTS rebuild) refuses, logs one
ERROR, and changes nothing; that with sole access the repair still heals and
backs up through SQLite's online backup API; and that opening a SessionDB no
longer drops another connection's lock.
"""
import logging
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import hermes_state
from hermes_state import (
    STATE_DB_OFFLINE_REPAIR_MARKER,
    SessionDB,
    repair_state_db_schema,
)

CORRUPT_BLOCK = bytes.fromhex("DEADBEEFDEADBEEF")

HOLDER = (
    "import sqlite3, sys, time\n"
    "c = sqlite3.connect(sys.argv[1])\n"
    "c.execute('SELECT COUNT(*) FROM sessions').fetchone()\n"
    "print('holding', flush=True)\n"
    "time.sleep(120)\n"
)

TRY_EXCLUSIVE = (
    "import sqlite3, sys\n"
    "g = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=0.2)\n"
    "g.execute('PRAGMA locking_mode=EXCLUSIVE')\n"
    "try:\n"
    "    g.execute('BEGIN EXCLUSIVE')\n"
    "    print('acquired')\n"
    "except sqlite3.Error as exc:\n"
    "    print('refused:', exc)\n"
)


def _build(db_path: Path) -> str:
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(5):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(sid, role="assistant", content=f"reply about pizza {i}")
    db.close()
    return sid


def _corrupt_fts_data(conn: sqlite3.Connection) -> None:
    """What Deo's state.db had: a damaged messages_fts_data; every append fails."""
    conn.execute("UPDATE messages_fts_data SET block = ?", (CORRUPT_BLOCK,))
    conn.commit()


def _fts_still_corrupt(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        blocks = {row[0] for row in conn.execute("SELECT block FROM messages_fts_data")}
    finally:
        conn.close()
    return blocks == {CORRUPT_BLOCK}


def _backups(db_path: Path) -> list:
    return sorted(db_path.parent.glob(f"{db_path.name}.malformed-backup-*"))


def _errors(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.fixture
def holder(tmp_path):
    """Hold a database open the way a live gateway does (WAL: a SHARED lock)."""
    conns = []

    def hold(db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        conns.append(conn)
        return conn

    yield hold
    for conn in conns:
        conn.close()


@pytest.fixture
def other_process():
    procs = []

    def hold(db_path: Path) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(db_path)],
            stdout=subprocess.PIPE, text=True,
        )
        assert proc.stdout.readline().strip() == "holding"
        procs.append(proc)
        return proc

    yield hold
    for proc in procs:
        proc.kill()
        proc.wait()


def _exclusive_from_another_process(db_path: Path) -> str:
    return subprocess.run(
        [sys.executable, "-c", TRY_EXCLUSIVE, str(db_path)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()


# ── the guard ────────────────────────────────────────────────────────────


def test_sole_access_is_refused_while_another_connection_has_the_database_open(tmp_path, holder):
    db_path = tmp_path / "state.db"
    _build(db_path)
    held = holder(db_path)

    guard, reason = hermes_state._open_sole_access(db_path)
    assert guard is None
    assert "locked" in reason

    held.close()
    guard, reason = hermes_state._open_sole_access(db_path)
    assert guard is not None and reason == ""
    try:
        # While the guard is open nobody else can even read the database.
        late = sqlite3.connect(str(db_path), timeout=0.1)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            late.execute("SELECT COUNT(*) FROM sessions").fetchone()
        late.close()
    finally:
        guard.close()
    after = sqlite3.connect(str(db_path))
    assert after.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    after.close()


def test_a_refused_guard_leaves_no_lock_behind(tmp_path, holder):
    db_path = tmp_path / "state.db"
    _build(db_path)
    held = holder(db_path)
    assert hermes_state._open_sole_access(db_path)[0] is None
    held.execute("INSERT INTO sessions (id, source, started_at) VALUES ('x', 'cli', 1)")
    held.commit()
    other = sqlite3.connect(str(db_path), timeout=0.1)
    assert other.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    other.close()


# ── repair_state_db_schema ───────────────────────────────────────────────


def test_repair_is_refused_and_changes_nothing_while_another_connection_holds_the_database(
    tmp_path, holder, caplog
):
    db_path = tmp_path / "state.db"
    _build(db_path)
    held = holder(db_path)
    _corrupt_fts_data(held)

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        report = repair_state_db_schema(db_path)

    assert report["repaired"] is False
    assert report["refused"] is True
    assert STATE_DB_OFFLINE_REPAIR_MARKER in report["error"]
    assert report["backup_path"] is None
    assert _backups(db_path) == []
    assert _fts_still_corrupt(db_path)
    assert len(_errors(caplog)) == 1
    assert STATE_DB_OFFLINE_REPAIR_MARKER in _errors(caplog)[0]
    assert "another connection or process has the database open" in _errors(caplog)[0]


def test_repair_is_refused_while_another_process_holds_the_database(
    tmp_path, other_process
):
    db_path = tmp_path / "state.db"
    _build(db_path)
    conn = sqlite3.connect(str(db_path))
    _corrupt_fts_data(conn)
    conn.close()
    proc = other_process(db_path)

    report = repair_state_db_schema(db_path)
    assert report["refused"] is True
    assert _fts_still_corrupt(db_path)

    proc.kill()
    proc.wait()
    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert report["strategy"] == "rebuild_fts"


def test_with_sole_access_the_repair_heals_and_backs_up_through_sqlite(tmp_path, monkeypatch):
    import shutil

    def no_raw_copies(*args, **kwargs):
        raise AssertionError("state.db must never be copied with a raw file descriptor")

    monkeypatch.setattr(shutil, "copy2", no_raw_copies)
    monkeypatch.setattr(shutil, "copyfile", no_raw_copies)
    db_path = tmp_path / "state.db"
    _build(db_path)
    conn = sqlite3.connect(str(db_path))
    _corrupt_fts_data(conn)
    conn.close()

    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert report["strategy"] == "rebuild_fts"
    assert hermes_state._db_opens_cleanly(db_path) is None

    # The backup is one self-contained SQLite file that preserves the damage.
    backup = Path(report["backup_path"])
    assert _backups(db_path) == [backup]
    assert not Path(str(backup) + "-wal").exists()
    copy = sqlite3.connect(str(backup))
    assert copy.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    assert {r[0] for r in copy.execute("SELECT block FROM messages_fts_data")} == {CORRUPT_BLOCK}
    copy.close()


def test_a_failed_backup_changes_nothing(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "state.db"
    _build(db_path)
    conn = sqlite3.connect(str(db_path))
    _corrupt_fts_data(conn)
    conn.close()
    monkeypatch.setattr(hermes_state, "_backup_db_online", lambda conn, path: None)

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        report = repair_state_db_schema(db_path)
    assert report["repaired"] is False
    assert "backup copy could not be written" in report["error"]
    assert _fts_still_corrupt(db_path)
    assert len(_errors(caplog)) == 1


# ── SessionDB open (malformed schema) ────────────────────────────────────


def test_open_time_repair_is_refused_while_the_database_is_held(tmp_path, holder, monkeypatch, caplog):
    db_path = tmp_path / "state.db"
    _build(db_path)
    held = holder(db_path)
    held.execute("PRAGMA writable_schema=ON")
    held.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
        "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master WHERE name='messages_fts'"
    )
    held.commit()
    monkeypatch.setattr(hermes_state, "_repair_attempted_paths", set())

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            SessionDB(db_path=db_path)

    errors = _errors(caplog)
    assert len(errors) == 1 and STATE_DB_OFFLINE_REPAIR_MARKER in errors[0]
    assert _backups(db_path) == []
    held.close()
    check = sqlite3.connect(str(db_path))
    check.execute("PRAGMA writable_schema=ON")
    assert check.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='messages_fts'"
    ).fetchone()[0] == 2  # untouched: the duplicate is still there
    check.close()


# ── SessionDB runtime FTS rebuild ────────────────────────────────────────


def test_runtime_fts_rebuild_is_refused_while_another_connection_holds_the_database(
    tmp_path, holder, caplog
):
    db_path = tmp_path / "state.db"
    sid = _build(db_path)
    db = SessionDB(db_path=db_path)
    try:
        held = holder(db_path)
        _corrupt_fts_data(held)

        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            with pytest.raises(sqlite3.DatabaseError):
                db.append_message(sid, role="user", content="lost while the index is corrupt")

        errors = _errors(caplog)
        assert len(errors) == 1 and STATE_DB_OFFLINE_REPAIR_MARKER in errors[0]
        assert "FTS index that rejects writes" in errors[0]
        assert _fts_still_corrupt(db_path)
        # The instance reconnected and still serves the canonical rows.
        assert len(db.get_messages(sid)) == 10
    finally:
        db.close()


def test_runtime_fts_rebuild_still_heals_with_sole_access(tmp_path):
    db_path = tmp_path / "state.db"
    sid = _build(db_path)
    db = SessionDB(db_path=db_path)
    try:
        conn = sqlite3.connect(str(db_path))
        _corrupt_fts_data(conn)
        conn.close()

        db.append_message(sid, role="user", content="saved after the rebuild pizza")
        assert len(db.get_messages(sid)) == 11
        hits = db._conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'pizza'"
        ).fetchone()[0]
        assert hits == 6
    finally:
        db.close()


def test_rebuild_fts_reconnects_with_the_same_connection_settings(tmp_path):
    db_path = tmp_path / "state.db"
    _build(db_path)
    db = SessionDB(db_path=db_path)
    try:
        # Runtime branch: native memory (memory_require_durable) is not on this base;
        # set the same durable-write state it sets.
        db._durable_writes = True
        db._conn.execute('PRAGMA synchronous=FULL')
        assert db.rebuild_fts() >= 1
        assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert isinstance(db._conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
    finally:
        db.close()


# ── SessionDB open never reads the file raw ──────────────────────────────


def test_opening_another_sessiondb_does_not_drop_a_live_connections_lock(tmp_path):
    """Closing a raw descriptor on state.db drops every POSIX lock this process
    holds on it. The zeroed-file check used to read the header on every open,
    so a second SessionDB in a gateway let another process believe the live
    gateway had the database closed, and take it exclusively."""
    db_path = tmp_path / "state.db"
    _build(db_path)
    live = SessionDB(db_path=db_path)
    try:
        live._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        SessionDB(db_path=db_path).close()
        assert _exclusive_from_another_process(db_path).startswith("refused")
    finally:
        live.close()
    assert _exclusive_from_another_process(db_path) == "acquired"


def test_a_healthy_open_does_not_read_the_header(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _build(db_path)

    def must_not_run(path, **kwargs):
        raise AssertionError("is_zeroed_state_db read a healthy state.db on open")

    monkeypatch.setattr(hermes_state, "is_zeroed_state_db", must_not_run)
    SessionDB(db_path=db_path).close()
