# tests/test_fts5_memory.py
"""FTS5+BM25 MemoryDB unit tests."""

import pytest
from tems.fts5_memory import MemoryDB


class TestMemoryDB:
    def test_commit_and_search(self, tmp_path):
        db = MemoryDB(str(tmp_path / "test.db"))
        rule_id = db.commit_memory(
            context_tags=["python", "test"],
            action_taken="wrote test",
            result="passed",
            correction_rule="always write tests first",
            keyword_trigger="python test TDD",
            category="TCL",
        )
        assert rule_id > 0

        results = db.search("python test")
        assert len(results) >= 1
        assert results[0]["category"] == "TCL"

    def test_commit_tcl(self, tmp_path):
        db = MemoryDB(str(tmp_path / "test.db"))
        rule_id = db.commit_tcl(
            original_instruction="앞으로 테스트 먼저 작성",
            topological_rule="TDD 의무화",
            keyword_trigger="TDD test 테스트",
            context_tags=["dev", "testing"],
        )
        assert rule_id > 0
        tcls = db.get_active_tcl()
        assert len(tcls) == 1
        assert tcls[0]["category"] == "TCL"

    def test_commit_tgl(self, tmp_path):
        db = MemoryDB(str(tmp_path / "test.db"))
        rule_id = db.commit_tgl(
            error_description="cp949 인코딩 에러",
            topological_case="Windows subprocess encoding",
            guard_rule="subprocess에서 bytes I/O 사용",
            keyword_trigger="subprocess encoding cp949 Windows",
            context_tags=["Windows", "encoding"],
        )
        assert rule_id > 0
        tgls = db.get_active_tgl()
        assert len(tgls) == 1
        assert tgls[0]["category"] == "TGL"

    def test_preflight(self, tmp_path):
        db = MemoryDB(str(tmp_path / "test.db"))
        db.commit_tcl("instruction", "rule1", "python import", ["dev"])
        db.commit_tgl("err", "case", "guard1", "encoding error", ["dev"])

        pf = db.preflight("python encoding")
        assert "tcl_hits" in pf
        assert "tgl_hits" in pf
        assert "general_hits" in pf

    def test_stats(self, tmp_path):
        db = MemoryDB(str(tmp_path / "test.db"))
        db.commit_memory(["tag"], "action", "result", category="TCL")
        db.commit_memory(["tag"], "action", "result", category="TGL")
        stats = db.stats()
        assert stats["total_records"] == 2
        assert "TCL" in stats["by_category"]
        assert "TGL" in stats["by_category"]

    def test_auto_summarize(self, tmp_path):
        db = MemoryDB(str(tmp_path / "test.db"))
        summary = db._auto_summarize("Windows subprocess 시: bytes I/O 사용하고 UTF-8로 디코딩")
        assert len(summary) > 0
        assert len(summary) <= 40
