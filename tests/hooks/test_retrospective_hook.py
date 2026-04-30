"""retrospective_hook.py — Stop event pattern-scan and output.

Hook contract (verified against src/tems/templates/retrospective_hook.py):
- Stop hook; ignores stdin (no sys.stdin.read() — uses argparse).
- Rate-limited by memory/.retrospective_last_run file (RATE_LIMIT_SEC=600).
- Calls pattern_detector.detect_patterns() and is_auto_register_enabled().
- If no candidates, exits 0 silently.
- If candidates found, prints <tems-retrospective ...> XML block to stdout.
- On exception: prints <retrospective-degraded .../> and exits 0.
- Side-effect (file written): memory/.retrospective_last_run (timestamp).

Plan deviation:
- Plan said "appends to memory/session_retrospectives.jsonl".
  Actual: no session_retrospectives.jsonl. Output goes to STDOUT only as
  <tems-retrospective> block. The only file written is .retrospective_last_run.
- To produce stdout output, need patterns in tool_failures.jsonl
  (>= REPETITION_THRESHOLD=3 identical signatures) AND rate-limit not in effect.
"""
import json
import time
from pathlib import Path

import pytest

from tests.hooks.conftest import run_hook


def _seed_tool_failures(agent_dir, count: int = 4) -> None:
    """Seed tool_failures.jsonl with identical failure entries to trigger pattern detection."""
    log_path = agent_dir / "memory" / "tool_failures.jsonl"
    entry = {
        "timestamp": "2026-01-01T10:00:00",
        "tool_name": "Bash",
        "cmd_summary": "python -c 'import missing_pkg'",
        "matches": [
            {
                "signature": "module_not_found",
                "severity": "critical",
                "detail": "missing_pkg",
            }
        ],
        "response_excerpt": "ModuleNotFoundError: No module named 'missing_pkg'",
    }
    with log_path.open("a", encoding="utf-8") as f:
        for _ in range(count):
            f.write(json.dumps(entry) + "\n")


def test_retrospective_hook_happy_path(agent_dir):
    """With 4 identical failures seeded, Stop event produces <tems-retrospective> stdout."""
    # Delete rate-limit file to ensure hook runs
    rate_file = agent_dir / "memory" / ".retrospective_last_run"
    if rate_file.exists():
        rate_file.unlink()

    _seed_tool_failures(agent_dir, count=4)

    out = run_hook(agent_dir, "retrospective_hook.py", {})
    raw = out.get("_raw_stdout", "")

    assert "<tems-retrospective" in raw, (
        f"Expected <tems-retrospective> block in stdout, got: {raw!r}"
    )
    assert "module_not_found" in raw or "missing_pkg" in raw, (
        f"Expected pattern signature in output: {raw!r}"
    )

    # Side-effect: rate-limit file must be updated
    assert rate_file.exists(), ".retrospective_last_run not written after execution"


def test_retrospective_hook_never_blocks_on_empty(agent_dir):
    """Empty event must exit 0 (hook ignores stdin entirely)."""
    out = run_hook(agent_dir, "retrospective_hook.py", {})
    # run_hook asserts returncode == 0.
    assert isinstance(out, dict)
    # No failures seeded → hook finds no candidates → silent exit.
    # Output is either empty or minimal.
