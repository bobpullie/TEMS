"""Shared fixtures for hook regression tests.

agent_dir: scaffolds a tmp agent and yields its root Path.
run_hook: subprocess wrapper that pipes JSON to a hook script and parses stdout.
"""
import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def agent_dir(tmp_path) -> Path:
    """Scaffold a tmp TEMS agent dir and return its root."""
    cwd = tmp_path / "agent"
    result = subprocess.run(
        [sys.executable, "-m", "tems.cli", "scaffold",
         "--agent-id", "test-agent",
         "--agent-name", "Test Agent",
         "--project", "TestProject",
         "--cwd", str(cwd)],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(result.stdout)
    assert out.get("ok"), f"scaffold failed: {out}"
    return cwd


def run_hook(agent_root: Path, script: str, event: dict, timeout: float = 10.0) -> dict:
    """Run a hook template with stdin event JSON, return parsed stdout dict.

    If stdout is empty (hook chose to inject nothing), returns {}.
    Always asserts exit code 0 (hooks must never block harness).
    """
    script_path = agent_root / "memory" / script
    assert script_path.exists(), f"Hook missing: {script_path}"

    env = os.environ.copy()
    # Many hooks resolve agent root via CWD walk; ensure CWD is the agent root.
    result = subprocess.run(
        [sys.executable, str(script_path)],
        input=json.dumps(event),
        capture_output=True, text=True, timeout=timeout,
        cwd=str(agent_root), env=env,
    )
    assert result.returncode == 0, (
        f"Hook {script} exited {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # preflight returns plaintext context, not JSON — wrap it
        return {"_raw_stdout": result.stdout}


def insert_rule(agent_root: Path, **kwargs) -> int:
    """Insert a TCL or TGL rule directly via MemoryDB."""
    from tems.fts5_memory import MemoryDB
    db = MemoryDB(str(agent_root / "memory" / "error_logs.db"))
    return db.commit_memory(**kwargs)


def query_db(agent_root: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Convenience: run a SELECT against the agent DB."""
    db_path = agent_root / "memory" / "error_logs.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
