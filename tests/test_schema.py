"""Schema single source of truth tests."""
import sqlite3
import tempfile
from pathlib import Path
import pytest


def _column_set(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_trigger_misses_schema_unified(tmp_path):
    """scaffold and tems_engine must produce identical trigger_misses schema.

    Regression: prior to schema SOT, scaffold defined
    (id, query, expected_rule_id, timestamp) while AdaptiveTrigger expected
    (prompt_text, missed_keywords, should_have_matched_rule_id, was_expanded,
    created_at) — INSERT crashed with OperationalError on first call.
    """
    from tems.fts5_memory import MemoryDB
    from tems.tems_engine import AdaptiveTrigger
    from tems.scaffold import _create_tables

    db_path = tmp_path / "test.db"

    # Simulate a scaffold run: this writes the OLD (wrong) trigger_misses schema
    # before AdaptiveTrigger ever sees the DB.
    _create_tables(str(db_path))

    # Now open MemoryDB and AdaptiveTrigger against the already-scaffolded DB.
    db = MemoryDB(str(db_path))
    trig = AdaptiveTrigger(db)  # _ensure_miss_table is a no-op (IF NOT EXISTS)

    with sqlite3.connect(str(db_path)) as conn:
        cols = _column_set(conn, "trigger_misses")

    expected = {
        "id", "prompt_text", "missed_keywords",
        "should_have_matched_rule_id", "was_expanded", "created_at",
    }
    assert expected.issubset(cols), (
        f"trigger_misses missing columns: {expected - cols}"
    )
