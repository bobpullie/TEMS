"""QMD rebuild tests — verify rule_*.md → DB restoration."""

import sqlite3
from pathlib import Path
from tems.rebuild_from_qmd import parse_qmd_rule, rebuild, resolve_agent_paths


class TestParseQmdRule:
    def test_valid_rule(self, tmp_path):
        rule_file = tmp_path / "rule_0001.md"
        rule_file.write_text("""---
rule_id: 1
category: TCL
tags: dev, testing
severity: directive
---

**Keywords:** TDD test pytest

**Rule:** Always write tests before implementation.

**Context:** [TCL] 원문: 앞으로 테스트 먼저 작성

**Result:** 위상적 변환 완료 → 규칙 활성화
""", encoding="utf-8")
        result = parse_qmd_rule(rule_file)
        assert result is not None
        assert result["rule_id"] == 1
        assert result["category"] == "TCL"
        assert "TDD" in result["keyword_trigger"]

    def test_invalid_no_frontmatter(self, tmp_path):
        rule_file = tmp_path / "rule_0002.md"
        rule_file.write_text("No frontmatter here", encoding="utf-8")
        result = parse_qmd_rule(rule_file)
        assert result is None

    def test_invalid_zero_id(self, tmp_path):
        rule_file = tmp_path / "rule_0000.md"
        rule_file.write_text("""---
rule_id: 0
category: TGL
---

**Keywords:** test
**Rule:** test rule
""", encoding="utf-8")
        result = parse_qmd_rule(rule_file)
        assert result is None


class TestResolveAgentPaths:
    def test_resolves_correctly(self, tmp_path):
        db_path, qmd_dir = resolve_agent_paths(tmp_path)
        assert db_path == tmp_path / "memory" / "error_logs.db"
        assert qmd_dir == tmp_path / "memory" / "qmd_rules"


class TestRebuild:
    def test_rebuild_empty_dir(self, tmp_path):
        qmd_dir = tmp_path / "qmd_rules"
        qmd_dir.mkdir()
        db_path = tmp_path / "test.db"
        result = rebuild(db_path, qmd_dir, dry_run=False)
        assert result["ok"] is True
        assert result["parsed"] == 0

    def test_rebuild_dry_run(self, tmp_path):
        qmd_dir = tmp_path / "qmd_rules"
        qmd_dir.mkdir()
        rule = qmd_dir / "rule_0001.md"
        rule.write_text("""---
rule_id: 1
category: TCL
tags: dev
severity: info
---

**Keywords:** test
**Rule:** test rule
**Context:** context
**Result:** result
""", encoding="utf-8")

        result = rebuild(tmp_path / "test.db", qmd_dir, dry_run=True)
        assert result["ok"] is True
        assert result["parsed"] == 1
        assert result["dry_run"] is True
        assert len(result["rules_preview"]) == 1

    def test_rebuild_inserts_into_db(self, tmp_path):
        # Create DB with schema first
        from tems.scaffold import create_directories, create_database
        create_directories(tmp_path)
        create_database(tmp_path, force=False)

        qmd_dir = tmp_path / "memory" / "qmd_rules"
        rule = qmd_dir / "rule_0001.md"
        rule.write_text("""---
rule_id: 1
category: TGL
tags: encoding
severity: error
---

**Keywords:** subprocess encoding cp949

**Rule:** Use bytes I/O for Windows subprocess

**Context:** [TGL] cp949 encoding crash

**Result:** Fixed with bytes mode
""", encoding="utf-8")

        db_path = tmp_path / "memory" / "error_logs.db"
        result = rebuild(db_path, qmd_dir, dry_run=False)
        assert result["ok"] is True
        assert result["inserted"] == 1

        # Verify in DB
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM memory_logs WHERE id = 1").fetchone()
        conn.close()
        assert row is not None
