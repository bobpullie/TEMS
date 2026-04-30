"""FTS5 idempotency + WAL regression tests."""
import sqlite3
import threading

from tems.fts5_memory import MemoryDB


def test_fts_rowid_preserved_across_reconstruction(tmp_path):
    """Reconstructing MemoryDB() must NOT wipe memory_fts (Critical #2 regression).

    Prior to PR-A's schema SOT, _init_db unconditionally DROPped memory_fts on
    every connection — search returned empty during the rebuild window and
    rebuilds were O(N) per process. After PR-A, schema.py's CREATE VIRTUAL TABLE
    IF NOT EXISTS makes _init_db idempotent. This test locks that in.
    """
    db_path = tmp_path / "fts.db"
    db1 = MemoryDB(str(db_path))
    rid = db1.commit_memory(
        context_tags=["test"],
        action_taken="action",
        result="result",
        correction_rule="rule about widgets",
        keyword_trigger="widget",
    )

    # Capture FTS rowids before reconstruction
    with sqlite3.connect(str(db_path)) as conn:
        before = conn.execute("SELECT rowid FROM memory_fts").fetchall()

    # Reconstruct (simulates a hook process opening a new MemoryDB)
    db2 = MemoryDB(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        after = conn.execute("SELECT rowid FROM memory_fts").fetchall()

    assert before == after, (
        f"FTS rowids changed across reconstruction: {before} -> {after}. "
        "Indicates _init_db is dropping/rebuilding memory_fts."
    )

    # Search must work after reconstruction
    hits = db2.search("widget")
    assert any(h["id"] == rid for h in hits)


def test_wal_mode_enabled(tmp_path):
    """Every connection should have journal_mode=WAL for hook concurrency."""
    db = MemoryDB(str(tmp_path / "wal.db"))
    with db._conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"Expected WAL, got {mode}"


def test_busy_timeout_set(tmp_path):
    """busy_timeout must be set so concurrent hooks don't immediately SQLITE_BUSY."""
    db = MemoryDB(str(tmp_path / "busy.db"))
    with db._conn() as conn:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 5000, f"Expected ≥5000ms, got {timeout}ms"


def test_concurrent_writers_no_busy_error(tmp_path):
    """Two threads writing simultaneously must not raise SQLITE_BUSY thanks
    to WAL + busy_timeout. Models the real workload of preflight + compliance
    racing on the same DB.
    """
    db_path = tmp_path / "concurrent.db"
    MemoryDB(str(db_path))  # initialize schema once

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer(tag: str):
        try:
            barrier.wait()
            db = MemoryDB(str(db_path))
            for i in range(20):
                db.commit_memory(
                    context_tags=[tag],
                    action_taken=f"act_{tag}_{i}",
                    result="r",
                    correction_rule=f"rule_{tag}_{i}",
                    keyword_trigger=f"kw_{tag}_{i}",
                )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=writer, args=("t1",))
    t2 = threading.Thread(target=writer, args=("t2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert errors == [], f"Concurrent writes raised: {errors}"

    # Both writers' rows present
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM memory_logs").fetchone()[0]
    assert count == 40
