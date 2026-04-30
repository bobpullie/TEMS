"""Schema single source of truth tests."""
import sqlite3


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


def test_adaptive_trigger_record_miss_works(tmp_path):
    """Regression: AdaptiveTrigger.record_miss crashed with OperationalError
    on fresh scaffold because trigger_misses had wrong column names.
    Now lands as part of the Critical #1 fix verification."""
    from tems.fts5_memory import MemoryDB
    from tems.tems_engine import AdaptiveTrigger
    from tems.scaffold import _create_tables

    db_path = tmp_path / "smoke.db"
    # Simulate scaffold-first ordering (real production flow)
    _create_tables(str(db_path))
    db = MemoryDB(str(db_path))
    trig = AdaptiveTrigger(db)
    # Should NOT raise — was OperationalError before A1-A5 chain
    trig.record_miss(
        prompt="some unmatched user prompt",
        missed_keywords=["foo", "bar"],
        rule_id=None,
    )
    stats = trig.get_miss_stats()
    assert stats["total_misses"] == 1


def test_apply_schema_idempotent(tmp_path):
    """apply_schema must be safely callable repeatedly."""
    from tems.schema import apply_schema, SCHEMA_VERSION

    db_path = tmp_path / "idem.db"
    for _ in range(3):
        with sqlite3.connect(str(db_path)) as conn:
            apply_schema(conn)
            conn.commit()
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            assert ver == SCHEMA_VERSION


def test_apply_schema_upgrades_legacy_db(tmp_path):
    """A pre-SOT DB missing fire_count etc. should gain them via _add_missing_columns."""
    from tems.schema import apply_schema

    db_path = tmp_path / "legacy.db"
    # Simulate pre-SOT schema: rule_health without fire_count
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE rule_health (
                rule_id INTEGER PRIMARY KEY,
                activation_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()

    with sqlite3.connect(str(db_path)) as conn:
        apply_schema(conn)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_health)").fetchall()}

    assert "fire_count" in cols
    assert "compliance_count" in cols
    assert "last_fired" in cols
