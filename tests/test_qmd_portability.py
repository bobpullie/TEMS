"""High #3 — qmd path detection / Universal Portability.

Verifies that `tems_engine` resolves the qmd CLI in the documented order
(TEMS_QMD_CMD env var → shutil.which → None) and that sync_rules_to_qmd
gracefully no-ops with a single diagnostic row when qmd is not installed.
"""
import json
import sys

import pytest

from tems import tems_engine
from tems.fts5_memory import MemoryDB


def test_resolve_qmd_cmd_prefers_env_var(monkeypatch):
    """TEMS_QMD_CMD env var must take precedence over PATH."""
    monkeypatch.setenv("TEMS_QMD_CMD", "/custom/path/to/qmd")
    assert tems_engine._resolve_qmd_cmd() == "/custom/path/to/qmd"


def test_resolve_qmd_cmd_falls_back_to_path(monkeypatch):
    """Without env var, shutil.which result is returned."""
    monkeypatch.delenv("TEMS_QMD_CMD", raising=False)
    monkeypatch.setattr(tems_engine.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    expected = "/usr/local/bin/qmd.cmd" if sys.platform == "win32" else "/usr/local/bin/qmd"
    assert tems_engine._resolve_qmd_cmd() == expected


def test_resolve_qmd_cmd_none_when_unavailable(monkeypatch):
    """No env var + PATH miss => None (signals qmd not installed)."""
    monkeypatch.delenv("TEMS_QMD_CMD", raising=False)
    monkeypatch.setattr(tems_engine.shutil, "which", lambda name: None)
    assert tems_engine._resolve_qmd_cmd() is None


def test_sync_rules_to_qmd_skips_when_qmd_missing(tmp_path, monkeypatch):
    """sync_rules_to_qmd must not crash when QMD_CMD is None and must emit one
    diagnostic row in audit_diagnostics_recent-compatible shape (dedupe across
    repeat calls in the same process)."""
    # Force QMD_CMD == None and reset the dedupe flag so we observe a fresh emit.
    monkeypatch.setattr(tems_engine, "QMD_CMD", None)
    monkeypatch.setattr(tems_engine, "_QMD_NOT_FOUND_LOGGED", False)

    db_path = tmp_path / "memory" / "error_logs.db"
    db_path.parent.mkdir(parents=True)
    db = MemoryDB(str(db_path))
    db.commit_memory(
        action_taken="[TGL] qmd portability fixture",
        result="needs at least one rule for sync to iterate",
        correction_rule="placeholder rule",
        keyword_trigger="placeholder",
        context_tags=[],
        category="TGL",
        severity="info",
    )

    qmd_rules_dir = tmp_path / "memory" / "qmd_rules"

    # First sync — must succeed and emit one diagnostic row.
    count = tems_engine.sync_rules_to_qmd(db, qmd_rules_dir)
    assert count >= 1, "sync should still write rule files even without qmd CLI"

    diag_path = tmp_path / "memory" / "tems_diagnostics.jsonl"
    assert diag_path.exists(), "qmd-not-found diagnostic must be emitted"
    rows = [json.loads(l) for l in diag_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    matches = [r for r in rows if r.get("event") == "qmd_command_not_found_failure"]
    assert len(matches) == 1, f"Expected exactly 1 diagnostic row, got: {rows}"
    row = matches[0]
    # Shape parity with audit_diagnostics_recent reader.
    assert row.get("event", "").endswith("_failure"), "event must end in _failure for reader filter"
    assert "exc_type" in row and "exc_msg" in row and "traceback" in row, (
        f"Diagnostic row missing audit-compatible keys, got: {row}"
    )

    # Second sync — must NOT add another row (dedupe).
    tems_engine.sync_rules_to_qmd(db, qmd_rules_dir)
    rows2 = [json.loads(l) for l in diag_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    matches2 = [r for r in rows2 if r.get("event") == "qmd_command_not_found_failure"]
    assert len(matches2) == 1, f"Dedupe failed — expected 1 row total, got: {len(matches2)}"
