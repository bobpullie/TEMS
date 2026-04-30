"""FTS5 idempotency + WAL regression tests."""
import sqlite3
import threading
import time
import pytest

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
