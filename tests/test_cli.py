"""CLI integration tests."""

import subprocess
import sys
import json
import pytest
from pathlib import Path


def run_tems(*args) -> subprocess.CompletedProcess:
    """Run `tems` CLI command."""
    return subprocess.run(
        [sys.executable, "-m", "tems.cli", *args],
        capture_output=True, text=True, timeout=30,
    )


class TestCLIScaffold:
    def test_scaffold_creates_agent(self, tmp_path):
        reg = tmp_path / "registry.json"
        result = run_tems(
            "scaffold",
            "--agent-id", "testagent",
            "--agent-name", "Test Agent",
            "--project", "TestProject",
            "--cwd", str(tmp_path / "agent"),
            "--registry-path", str(reg),
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["ok"] is True

        # Verify artifacts
        agent_dir = tmp_path / "agent"
        assert (agent_dir / ".claude" / "tems_agent_id").exists()
        assert (agent_dir / "memory" / "error_logs.db").exists()

    def test_scaffold_missing_args(self):
        result = run_tems("scaffold")
        assert result.returncode != 0


class TestCLIInitSkill:
    def test_init_skill_copies_files(self, tmp_path):
        target = tmp_path / "skill_target"
        result = run_tems("init-skill", "--target", str(target))
        assert result.returncode == 0
        assert (target / "SKILL.md").exists()
        assert (target / "references" / "tems-architecture.md").exists()


class TestCLIHelp:
    def test_help(self):
        result = run_tems("--help")
        assert result.returncode == 0
        assert "scaffold" in result.stdout
        assert "init-skill" in result.stdout
