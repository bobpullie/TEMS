"""decay.py — 30/90-day cold/archive status transitions.

Hook contract (verified against src/tems/templates/decay.py):
- CLI script; ignores stdin (uses argparse).
- Reads from memory/error_logs.db via rule_health JOIN memory_logs.
- Side-effect: UPDATE rule_health SET status = 'cold'/'archive', status_changed_at = now.
- Uses last_fired > last_activated > log_timestamp > status_changed_at as effective date.
- 30+ days without activity: warm -> cold.
- 90+ days without activity: warm/cold -> archive.
- Exits 1 when DB not found; exits 0 otherwise.
- When no transitions needed, exits 0 with summary line (no JSON unless --json passed).
- run_hook passes {} via stdin (script ignores it) and no CLI args (defaults apply).

Plan deviation:
- Plan said "seed rule with last_fired = 100 days ago, rule_health.status becomes 'cold'".
  At 100 days, transition is to 'archive' (>= ARCHIVE_DAYS=90), not 'cold'.
  Use 45 days ago for warm->cold; 100 days ago for warm->archive.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from tests.hooks.conftest import run_hook, insert_rule, query_db


def _seed_rule_health(agent_dir, rule_id: int, days_ago: int, status: str = "warm") -> None:
    """Insert a rule_health row with last_fired set to N days in the past."""
    db_path = agent_dir / "memory" / "error_logs.db"
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        # INSERT OR REPLACE in case a row already exists for rule_id
        conn.execute(
            """INSERT OR REPLACE INTO rule_health (rule_id, ths_score, status, last_fired)
               VALUES (?, 0.5, ?, ?)""",
            (rule_id, status, ts),
        )
        conn.commit()


def test_decay_cold_transition(agent_dir):
    """Rule inactive for 45 days transitions from 'warm' to 'cold'."""
    rid = insert_rule(
        agent_dir,
        context_tags=["classification:TCL"],
        action_taken="[TCL] stale rule",
        result="case: unused guideline",
        correction_rule="this rule should go cold after 30 days",
        keyword_trigger="stale cold decay warm",
        category="TCL",
        severity="info",
    )

    _seed_rule_health(agent_dir, rid, days_ago=45, status="warm")

    # Confirm initial status is warm
    rows = query_db(agent_dir, "SELECT status FROM rule_health WHERE rule_id = ?", (rid,))
    assert rows and rows[0]["status"] == "warm", f"Expected warm initial status: {rows}"

    # Run decay (no CLI args, so dry_run=False, no --json)
    out = run_hook(agent_dir, "decay.py", {})
    # Decay outputs plaintext text, run_hook wraps as _raw_stdout
    raw = out.get("_raw_stdout", "")
    # Some output expected (transition logged)
    assert "cold" in raw.lower() or "transition" in raw.lower() or "APPLIED" in raw, (
        f"Expected transition mention in output: {raw!r}"
    )

    rows = query_db(agent_dir, "SELECT status FROM rule_health WHERE rule_id = ?", (rid,))
    assert rows and rows[0]["status"] == "cold", (
        f"Expected status='cold' after 45-day decay, got: {rows}"
    )


def test_decay_never_blocks_on_empty(agent_dir):
    """Empty event (no stdin content used) must exit 0 even with no rules."""
    out = run_hook(agent_dir, "decay.py", {})
    # run_hook asserts returncode == 0.
    assert isinstance(out, dict)
    # With no rules needing transition, output is minimal but exit is 0.
