"""memory_bridge.py — PostToolUse Write/Edit -> TEMS DB auto-bridge.

Hook contract (verified against src/tems/templates/memory_bridge.py):
- Reads: tool_name ("Write" or "Edit"), tool_input.file_path from stdin JSON.
- Filters: file_path must be inside MEMORY_DIR (resolved via marker walk or TEMS_MEMORY_DIR).
  Check: str(MEMORY_DIR) in file_path.replace("/", "\\\\")
- Filters: file_path must not end with "MEMORY.md".
- Parses the target file: requires YAML frontmatter --- ... --- with 'type: feedback'.
- classify_rule: type must == 'feedback', otherwise returns not-feedback.
- bridge_to_tems: inserts into memory_logs + rule_health. Returns ok=True on success.
- Stdout: prints "[TEMS Bridge] {category} #{id} auto-registered..." on success.
- Exits 0 always (return statements, not sys.exit(1)).

Plan deviation:
- Plan said "new entry tagged with the changed file path".
  Actual: DB insertion tags include "source:memory-bridge:{name}" from frontmatter.
  The file path is not stored as a tag; the frontmatter 'name' field is used.
- MEMORY_DIR resolves via marker walk (no env var in conftest), so agent_dir/memory/ is used.
  The event file_path must use the actual absolute path of the agent's memory directory.

MEMORY_DIR path note:
  The hook's _resolve_memory_dir() walks up from Path(__file__).resolve().parent.
  Since the hook lives at agent_dir/memory/memory_bridge.py, it finds the
  .claude/tems_agent_id marker at agent_dir/.claude/ and returns agent_dir/memory.
  The path comparison uses str(MEMORY_DIR), which is the OS-resolved absolute path.
"""
import json
import sqlite3

import pytest

from tests.hooks.conftest import run_hook, query_db

FEEDBACK_CONTENT = """\
---
name: test-feedback-rule
description: never skip pip verification
type: feedback
---
**Rule:** Never skip pip verification before importing a package.

**Why:** Missing packages cause ModuleNotFoundError silently in some contexts.

**How to apply:** Always run pip show <pkg> before importing in new environments.
"""


def test_memory_bridge_happy_path(agent_dir):
    """Write event for a feedback file in memory/ creates a TCL/TGL row in memory_logs."""
    # Write a feedback file inside the agent's memory directory
    feedback_path = agent_dir / "memory" / "test_feedback.md"
    feedback_path.write_text(FEEDBACK_CONTENT, encoding="utf-8")

    # file_path must use the same path representation the hook sees.
    # Use str(feedback_path) so the path comparison in the hook succeeds.
    event = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(feedback_path),
        },
        "tool_response": "File written successfully.",
    }
    out = run_hook(agent_dir, "memory_bridge.py", event)

    raw = out.get("_raw_stdout", "")
    assert "[TEMS Bridge]" in raw, (
        f"Expected '[TEMS Bridge]' in stdout, got: {raw!r}"
    )
    assert "auto-registered" in raw, f"Expected 'auto-registered' in stdout: {raw!r}"

    # Verify DB row created
    rows = query_db(
        agent_dir,
        "SELECT id, category, context_tags FROM memory_logs WHERE context_tags LIKE '%source:memory-bridge%'",
    )
    assert len(rows) >= 1, (
        f"Expected memory_logs row with source:memory-bridge tag. Rows: {rows}"
    )
    assert rows[0]["category"] in ("TGL", "TCL"), (
        f"Unexpected category: {rows[0]['category']}"
    )


def test_memory_bridge_never_blocks_on_empty(agent_dir):
    """Empty event must exit 0 (hook returns silently on JSON parse failure)."""
    out = run_hook(agent_dir, "memory_bridge.py", {})
    # run_hook asserts returncode == 0.
    assert isinstance(out, dict)
    # No valid tool_name → hook returns immediately with no output.
    assert out == {}, f"Expected empty output, got: {out}"
