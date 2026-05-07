"""TEMS schema — single source of truth for all DB DDL.

Every CREATE TABLE / VIRTUAL TABLE / TRIGGER / INDEX in the TEMS package MUST
live here. Modules that previously declared inline DDL now call apply_schema().

Migrations: bump SCHEMA_VERSION and add a step to MIGRATIONS. The runner uses
PRAGMA user_version (SQLite-native, no auxiliary table needed).
"""
import sqlite3
from typing import Callable

__all__ = ["SCHEMA_VERSION", "MIGRATIONS", "apply_schema"]

SCHEMA_VERSION = 2  # bump when adding a migration step

# ─── Base tables (version 1) ────────────────────────────────────────────────

_BASE_TABLES = {
    "memory_logs": """
        CREATE TABLE IF NOT EXISTS memory_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            context_tags TEXT NOT NULL DEFAULT '',
            keyword_trigger TEXT DEFAULT '',
            action_taken TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            correction_rule TEXT,
            summary TEXT DEFAULT '',
            category TEXT DEFAULT 'general',
            severity TEXT DEFAULT 'info',
            embedding BLOB DEFAULT NULL,
            superseded_by TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """,
    "rule_health": """
        CREATE TABLE IF NOT EXISTS rule_health (
            rule_id INTEGER PRIMARY KEY,
            activation_count INTEGER DEFAULT 0,
            fire_count INTEGER DEFAULT 0,
            compliance_count INTEGER DEFAULT 0,
            violation_count INTEGER DEFAULT 0,
            correction_success INTEGER DEFAULT 0,
            correction_total INTEGER DEFAULT 0,
            modification_count INTEGER DEFAULT 0,
            last_activated TEXT,
            last_fired TEXT,
            last_modified TEXT,
            classification TEXT,
            needs_review INTEGER DEFAULT 0,
            status TEXT DEFAULT 'warm',
            status_changed_at TEXT DEFAULT (datetime('now')),
            ths_score REAL DEFAULT 0.5,
            ths_updated_at TEXT DEFAULT (datetime('now')),
            abstraction_level TEXT,
            -- S60 compliance reform
            active_compliance_count INTEGER DEFAULT 0,
            last_active_compliance_at TEXT,
            relevance_skipped_count INTEGER DEFAULT 0,
            FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
        )
    """,
    "exceptions": """
        CREATE TABLE IF NOT EXISTS exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER,
            exception_type TEXT NOT NULL,
            description TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            persistence_score REAL DEFAULT 0.0,
            created_at TEXT DEFAULT (datetime('now')),
            last_seen TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'active',
            promoted_to_rule_id INTEGER,
            FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
        )
    """,
    "meta_rules": """
        CREATE TABLE IF NOT EXISTS meta_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level INTEGER NOT NULL,
            parameter_name TEXT NOT NULL,
            old_value REAL,
            new_value REAL,
            reason TEXT,
            system_health_before REAL,
            system_health_after REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """,
    "rule_edges": """
        CREATE TABLE IF NOT EXISTS rule_edges (
            rule_a INTEGER NOT NULL,
            rule_b INTEGER NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL DEFAULT 0.0,
            updated_at TEXT DEFAULT (datetime('now')),
            valid_from TEXT DEFAULT NULL,
            valid_until TEXT DEFAULT NULL,
            PRIMARY KEY (rule_a, rule_b, edge_type)
        )
    """,
    "co_activations": """
        CREATE TABLE IF NOT EXISTS co_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_hash TEXT NOT NULL,
            rule_ids TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """,
    "tgl_sequences": """
        CREATE TABLE IF NOT EXISTS tgl_sequences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predecessor_id INTEGER NOT NULL,
            successor_id INTEGER NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            confidence REAL DEFAULT 0.0,
            last_seen TEXT DEFAULT (datetime('now')),
            UNIQUE(predecessor_id, successor_id)
        )
    """,
    # CRITICAL #1 fix: unified to AdaptiveTrigger's expected shape
    "trigger_misses": """
        CREATE TABLE IF NOT EXISTS trigger_misses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_text TEXT NOT NULL,
            missed_keywords TEXT NOT NULL,
            should_have_matched_rule_id INTEGER,  -- nullable: engine passes None on unknown-rule misses
            was_expanded INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """,
    "rule_versions": """
        CREATE TABLE IF NOT EXISTS rule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            snapshot_json TEXT NOT NULL,
            changed_fields TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
        )
    """,
    "embedding_meta": """
        CREATE TABLE IF NOT EXISTS embedding_meta (
            rule_id INTEGER PRIMARY KEY,
            model_id TEXT NOT NULL,
            dim INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (rule_id) REFERENCES memory_logs(id) ON DELETE CASCADE
        )
    """,
}

# ─── FTS5 virtual table + triggers (CRITICAL #3 fix: includes summary) ──────

_FTS_DDL = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        context_tags,
        keyword_trigger,
        action_taken,
        result,
        correction_rule,
        summary,
        category,
        content=memory_logs,
        content_rowid=id,
        tokenize='unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_logs BEGIN
        INSERT INTO memory_fts(rowid, context_tags, keyword_trigger,
            action_taken, result, correction_rule, summary, category)
        VALUES (new.id, new.context_tags, new.keyword_trigger,
            new.action_taken, new.result, new.correction_rule,
            COALESCE(new.summary, ''), new.category);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_logs BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, context_tags, keyword_trigger,
            action_taken, result, correction_rule, summary, category)
        VALUES ('delete', old.id, old.context_tags, old.keyword_trigger,
            old.action_taken, old.result, old.correction_rule,
            COALESCE(old.summary, ''), old.category);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_logs BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, context_tags, keyword_trigger,
            action_taken, result, correction_rule, summary, category)
        VALUES ('delete', old.id, old.context_tags, old.keyword_trigger,
            old.action_taken, old.result, old.correction_rule,
            COALESCE(old.summary, ''), old.category);
        INSERT INTO memory_fts(rowid, context_tags, keyword_trigger,
            action_taken, result, correction_rule, summary, category)
        VALUES (new.id, new.context_tags, new.keyword_trigger,
            new.action_taken, new.result, new.correction_rule,
            COALESCE(new.summary, ''), new.category);
    END
    """,
]

# ─── Migration runner ───────────────────────────────────────────────────────

# Each migration: function (conn) -> None that brings schema from version N to N+1.
# v0 -> v1: initial schema creation + defensive column additions for pre-SOT DBs.
# Idempotent via CREATE TABLE IF NOT EXISTS + ALTER TABLE catch-on-duplicate.

def _add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    cols: list[tuple[str, str, str]],
) -> None:
    """Add columns that don't yet exist. Defaults must be SQLite constants."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, typ, default in cols:
        if name in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {typ} DEFAULT {default}"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower() and "already exists" not in str(exc).lower():
                raise


def _apply_schema_v1(conn: sqlite3.Connection) -> None:
    for ddl in _BASE_TABLES.values():
        conn.execute(ddl)
    # Defensive ALTER TABLE for upgrades from pre-SOT DBs that may lack columns.
    # IF NOT EXISTS not supported on ADD COLUMN; use try/except.
    _add_missing_columns(conn, "memory_logs", [
        ("summary", "TEXT", "''"),
        ("embedding", "BLOB", "NULL"),
        ("superseded_by", "TEXT", "NULL"),
        ("valid_from", "TEXT", "NULL"),
        ("valid_until", "TEXT", "NULL"),
    ])
    _add_missing_columns(conn, "rule_health", [
        ("fire_count", "INTEGER", "0"),
        ("compliance_count", "INTEGER", "0"),
        ("violation_count", "INTEGER", "0"),
        ("last_fired", "TEXT", "NULL"),
        ("classification", "TEXT", "NULL"),
        ("needs_review", "INTEGER", "0"),
        ("abstraction_level", "TEXT", "NULL"),
        # S60 compliance reform — active citation channel + scope gate monitoring
        ("active_compliance_count", "INTEGER", "0"),
        ("last_active_compliance_at", "TEXT", "NULL"),
        ("relevance_skipped_count", "INTEGER", "0"),
    ])
    _add_missing_columns(conn, "rule_edges", [
        ("valid_from", "TEXT", "NULL"),
        ("valid_until", "TEXT", "NULL"),
    ])
    for ddl in _FTS_DDL:
        conn.execute(ddl)


def _apply_schema_v2(conn: sqlite3.Connection) -> None:
    """v2 — S60 compliance reform: active citation channel + scope gate counters.

    구 DB (v1 도달) 가 v2 로 올라올 때 새 컬럼 3종을 추가한다. v1 신규 생성 DB
    는 _apply_schema_v1 의 _add_missing_columns 에 이미 동일 항목이 적힘 —
    중복 호출이지만 try/except 가 'duplicate column' 을 흡수.
    """
    _add_missing_columns(conn, "rule_health", [
        ("active_compliance_count", "INTEGER", "0"),
        ("last_active_compliance_at", "TEXT", "NULL"),
        ("relevance_skipped_count", "INTEGER", "0"),
    ])


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _apply_schema_v1,  # index 0 = migrate to v1
    _apply_schema_v2,  # index 1 = migrate to v2 (S60 compliance reform columns)
]


def apply_schema(conn: sqlite3.Connection) -> None:
    """Bring connection's DB schema up to SCHEMA_VERSION. Idempotent.

    Caller is responsible for opening conn and committing after.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for v in range(current, SCHEMA_VERSION):
        MIGRATIONS[v](conn)
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
