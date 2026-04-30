"""pattern_detector.py — Scan jsonl channels, detect repeated failure patterns.

Hook contract (verified against src/tems/templates/pattern_detector.py):
- CLI script with argparse; ignores stdin entirely.
- MEMORY_DIR = Path(__file__).resolve().parent (the memory/ folder).
- Reads memory/tool_failures.jsonl + memory/tems_diagnostics.jsonl.
- REPETITION_THRESHOLD = 3: minimum count to surface a candidate.
- AUTO_REGISTER_THRESHOLD = 5: minimum count for auto-register (requires TCL enable flag).
- Auto-register mode: inserts a TGL row via tems_commit.py subprocess.
- Without --json flag, outputs human-readable text.
- Exits 0 always (even when no candidates found).

Plan deviation:
- Plan said "PostToolUse Bash failure → after 5 identical failures, new TGL row
  in memory_logs with category='TGL' and needs_review=1".
  Actual: no 'needs_review' column in memory_logs schema.
  Auto-register inserts via tems_commit.py: category='TGL', context_tags includes
  'auto-detected', no needs_review field.
  Requires: (a) TCL row with 'auto-register-enabled' or 'TEMS 자동등록 활성화' in
  correction_rule/keyword_trigger, plus (b) 5+ identical failures in tool_failures.jsonl.
"""
import json
import sqlite3

import pytest

from tests.hooks.conftest import run_hook, insert_rule, query_db


def _seed_tool_failures(agent_dir, count: int) -> None:
    """Write identical failure entries to memory/tool_failures.jsonl."""
    log_path = agent_dir / "memory" / "tool_failures.jsonl"
    entry = {
        "timestamp": "2026-01-01T10:00:00",
        "tool_name": "Bash",
        "cmd_summary": "python -c 'import auto_pkg'",
        "matches": [
            {
                "signature": "module_not_found",
                "severity": "critical",
                "detail": "auto_pkg",
            }
        ],
        "response_excerpt": "ModuleNotFoundError: No module named 'auto_pkg'",
    }
    with log_path.open("a", encoding="utf-8") as f:
        for _ in range(count):
            f.write(json.dumps(entry) + "\n")


def _enable_auto_register(agent_dir) -> int:
    """Insert a TCL row that enables auto-register mode."""
    return insert_rule(
        agent_dir,
        context_tags=["classification:TCL"],
        action_taken="[TCL] 자동등록 활성화",
        result="auto-register enabled for testing",
        correction_rule="auto-register-enabled TEMS 자동등록 활성화",
        keyword_trigger="TEMS 자동등록 활성화 auto-register-enabled",
        category="TCL",
        severity="info",
    )


def test_pattern_detector_happy_path(agent_dir):
    """With 5+ identical failures and auto-register enabled, a TGL row is
    inserted into memory_logs with category='TGL' and auto-detected tag.
    """
    _enable_auto_register(agent_dir)
    _seed_tool_failures(agent_dir, count=5)

    out = run_hook(agent_dir, "pattern_detector.py", {})
    # Script outputs human-readable text (no --json flag via run_hook).
    raw = out.get("_raw_stdout", "")

    # Either auto-register succeeded or pattern was found (could be already-registered).
    # Check: a TGL row with auto-detected tag was inserted.
    tgl_rows = query_db(
        agent_dir,
        "SELECT id, category, context_tags FROM memory_logs WHERE category = 'TGL' AND context_tags LIKE '%auto-detected%'",
    )
    assert len(tgl_rows) >= 1, (
        f"Expected at least 1 TGL row with auto-detected tag.\n"
        f"stdout: {raw!r}\n"
        f"All TGL rows: {query_db(agent_dir, 'SELECT id, category, context_tags FROM memory_logs WHERE category=?', ('TGL',))}"
    )


def test_pattern_detector_never_blocks_on_empty(agent_dir):
    """No failure data → script finds no candidates → exits 0 silently."""
    out = run_hook(agent_dir, "pattern_detector.py", {})
    # run_hook asserts returncode == 0.
    assert isinstance(out, dict)
    # No candidates → no output (or empty output).
