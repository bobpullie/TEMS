"""End-to-end integration: scaffold → commit → preflight → rebuild cycle."""

import json
import subprocess
import sys
from pathlib import Path
from tems.fts5_memory import MemoryDB
from tems.scaffold import create_marker, create_directories, create_database, copy_templates


def test_full_lifecycle(tmp_path):
    """scaffold → commit rule → search → rebuild cycle."""
    agent_dir = tmp_path / "test_agent"
    agent_dir.mkdir()

    # 1. Scaffold
    create_marker(agent_dir, "integration_test", force=False)
    create_directories(agent_dir)
    create_database(agent_dir, force=False)

    # 2. Commit rules via MemoryDB
    db_path = agent_dir / "memory" / "error_logs.db"
    db = MemoryDB(str(db_path))

    tcl_id = db.commit_tcl(
        original_instruction="앞으로 TDD 필수",
        topological_rule="모든 구현 전 테스트 작성",
        keyword_trigger="TDD test 테스트 구현",
        context_tags=["dev", "testing"],
    )
    assert tcl_id > 0

    tgl_id = db.commit_tgl(
        error_description="subprocess cp949 crash",
        topological_case="Windows encoding boundary",
        guard_rule="bytes I/O + manual UTF-8 decode",
        keyword_trigger="subprocess encoding Windows cp949",
        context_tags=["Windows", "subprocess"],
    )
    assert tgl_id > 0

    # 3. Preflight search — use individual terms; FTS5 phrase-matches multi-word strings
    pf = db.preflight("subprocess")
    all_hits = pf["tcl_hits"] + pf["tgl_hits"] + pf["general_hits"]
    assert len(all_hits) >= 1

    # 4. Stats
    stats = db.stats()
    assert stats["total_records"] == 2


def test_cli_scaffold_e2e(tmp_path):
    """CLI scaffold creates working agent environment."""
    agent_dir = tmp_path / "cli_agent"
    reg_path = tmp_path / "registry.json"

    result = subprocess.run(
        [sys.executable, "-m", "tems.cli",
         "scaffold",
         "--agent-id", "e2e_test",
         "--agent-name", "E2E Test",
         "--project", "TestProject",
         "--cwd", str(agent_dir),
         "--registry-path", str(reg_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["ok"] is True

    # Verify DB is usable
    db = MemoryDB(str(agent_dir / "memory" / "error_logs.db"))
    rid = db.commit_memory(["test"], "test action", "test result")
    assert rid > 0

    # Verify registry
    registry = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "e2e_test" in registry["agents"]
