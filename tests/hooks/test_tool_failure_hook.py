"""tool_failure_hook.py — PostToolUse auto-failure detection.

Hook contract (verified against src/tems/templates/tool_failure_hook.py):
- Reads: tool_name, tool_input, tool_response from stdin JSON.
- Matches tool_response against FAILURE_SIGNATURES (regex list).
- Side-effect: appends a JSON record to memory/tool_failures.jsonl.
- Also emits a <tool-failure-detected> block to stdout (plaintext, not JSON).
- Exits 0 always (exit 0 on no-match, no-input, or after writing).
- is_ignored() skips events where tool_input.command matches IGNORE_PATTERNS.

Plan deviation:
- Plan said "new entry in memory/tems_diagnostics.jsonl with event='bash_failure'".
  Actual output is memory/tool_failures.jsonl (not tems_diagnostics.jsonl).
  The fields are: timestamp, tool_name, cmd_summary, matches, response_excerpt.
"""
import json
from pathlib import Path

import pytest

from tests.hooks.conftest import run_hook


def test_tool_failure_hook_happy_path(agent_dir):
    """PostToolUse Bash event with a ModuleNotFoundError in tool_response
    produces a new entry in memory/tool_failures.jsonl.
    """
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -c 'import missing_pkg'"},
        "tool_response": "ModuleNotFoundError: No module named 'missing_pkg'",
    }
    out = run_hook(agent_dir, "tool_failure_hook.py", event)

    # Hook emits plaintext, not JSON — run_hook wraps it.
    raw = out.get("_raw_stdout", "")
    assert "<tool-failure-detected>" in raw, f"Expected alert block in stdout: {raw!r}"
    assert "module_not_found" in raw or "ModuleNotFoundError" in raw, raw

    # Verify persistence in tool_failures.jsonl
    log_path = agent_dir / "memory" / "tool_failures.jsonl"
    assert log_path.exists(), "tool_failures.jsonl not created"
    lines = [l for l in log_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
    assert len(lines) >= 1, "Expected at least one log entry"
    record = json.loads(lines[-1])
    assert record["tool_name"] == "Bash"
    assert any(m["signature"] == "module_not_found" for m in record["matches"])


def test_tool_failure_hook_never_blocks_on_empty(agent_dir):
    """Empty event must exit 0 and produce no output."""
    out = run_hook(agent_dir, "tool_failure_hook.py", {})
    # Hook exits 0 on empty/unparseable input — run_hook asserts returncode==0.
    assert isinstance(out, dict)
    # No output expected: hook returns immediately on empty tool_response.
    assert out == {}, f"Expected empty output, got: {out}"
