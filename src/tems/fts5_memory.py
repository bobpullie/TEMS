"""
위상군(WesangGoon) FTS5+BM25 장기기억 시스템 v2
================================================
SQLite FTS5 기반 오류 로그 및 진행 기록 검색 엔진.
BM25 랭킹으로 과거 실패/성공 패턴을 자동 검색합니다.

카테고리:
  - TCL (Topological Checklist Loop): 사용자 "앞으로" 지시 → 위상적 체크리스트
  - TGL (Topological Guard Loop): 실수/시행착오 → 위상적 가드 규칙
  - session: 세션 메타데이터
  - general: 일반 기록

Usage:
    from tems.fts5_memory import MemoryDB
    db = MemoryDB()
    db.commit_tcl(...)
    db.commit_tgl(...)
    db.preflight("query")
    db.search("query")
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = None


class MemoryDB:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    context_tags TEXT NOT NULL,
                    keyword_trigger TEXT DEFAULT '',
                    action_taken TEXT NOT NULL,
                    result TEXT NOT NULL,
                    correction_rule TEXT,
                    category TEXT DEFAULT 'general',
                    severity TEXT DEFAULT 'info',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)

            for col, default in [
                ("keyword_trigger", "''"),
                ("summary", "''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memory_logs ADD COLUMN {col} TEXT DEFAULT {default}")
                except sqlite3.OperationalError:
                    pass

            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute("DROP TRIGGER IF EXISTS memory_ai")
            conn.execute("DROP TRIGGER IF EXISTS memory_ad")
            conn.execute("DROP TRIGGER IF EXISTS memory_au")

            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    context_tags,
                    keyword_trigger,
                    action_taken,
                    result,
                    correction_rule,
                    category,
                    content=memory_logs,
                    content_rowid=id,
                    tokenize='unicode61'
                )
            """)

            conn.execute("""
                INSERT INTO memory_fts(rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category)
                SELECT id, context_tags, COALESCE(keyword_trigger, ''), action_taken, result, correction_rule, category
                FROM memory_logs
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_logs BEGIN
                    INSERT INTO memory_fts(rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category)
                    VALUES (new.id, new.context_tags, new.keyword_trigger, new.action_taken, new.result, new.correction_rule, new.category);
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_logs BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category)
                    VALUES ('delete', old.id, old.context_tags, old.keyword_trigger, old.action_taken, old.result, old.correction_rule, old.category);
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_logs BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category)
                    VALUES ('delete', old.id, old.context_tags, old.keyword_trigger, old.action_taken, old.result, old.correction_rule, old.category);
                    INSERT INTO memory_fts(rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category)
                    VALUES (new.id, new.context_tags, new.keyword_trigger, new.action_taken, new.result, new.correction_rule, new.category);
                END
            """)

            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _auto_summarize(correction_rule: str, max_len: int = 40) -> str:
        if not correction_rule:
            return ""
        text = correction_rule.strip()
        if "시:" in text:
            text = text.split("시:")[-1].strip()
            if "(1)" in text:
                text = text.split("(1)")[0].strip()
                if not text:
                    parts = correction_rule.split("(1)")
                    if len(parts) > 1:
                        text = parts[1].split("(2)")[0].strip()
        if len(text) > max_len:
            for sep in [".", "。", ",", "，", " — ", " - "]:
                idx = text.find(sep, max_len // 2)
                if 0 < idx <= max_len:
                    text = text[:idx]
                    break
            else:
                text = text[:max_len]
        return text.strip(" ,;:→")

    def commit_memory(
        self,
        context_tags: list[str],
        action_taken: str,
        result: str,
        correction_rule: str = "",
        keyword_trigger: str = "",
        category: str = "general",
        severity: str = "info",
        summary: str = "",
        timestamp: Optional[str] = None,
    ) -> int:
        ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tags_str = ", ".join(context_tags)
        if not summary:
            summary = self._auto_summarize(correction_rule)

        # keyword_trigger 자동 보완 — 한국어 어간 추가 (BM25 매칭 강화)
        if keyword_trigger:
            from tems.korean_utils import strip_korean_suffix  # lazy import (순환 import 회피)
            tokens = keyword_trigger.split()
            extras = []
            seen = set(t.lower() for t in tokens)
            for tok in tokens:
                stem = strip_korean_suffix(tok)
                if stem != tok and len(stem) > 1 and stem.lower() not in seen:
                    extras.append(stem)
                    seen.add(stem.lower())
            if extras:
                keyword_trigger = keyword_trigger + " " + " ".join(extras)

        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory_logs
                    (timestamp, context_tags, keyword_trigger, action_taken, result, correction_rule, summary, category, severity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, tags_str, keyword_trigger, action_taken, result, correction_rule, summary, category, severity),
            )
            conn.commit()
            return cursor.lastrowid

    def search(self, query: str, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id, m.timestamp, m.context_tags, m.keyword_trigger,
                    m.action_taken, m.result, m.correction_rule,
                    m.summary, m.category, m.severity, rank
                FROM memory_fts f
                JOIN memory_logs m ON f.rowid = m.id
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def commit_tcl(
        self,
        original_instruction: str,
        topological_rule: str,
        keyword_trigger: str,
        context_tags: list[str],
    ) -> int:
        return self.commit_memory(
            context_tags=context_tags,
            action_taken=f"[TCL] 원문: {original_instruction}",
            result=f"위상적 변환 완료 → 규칙 활성화",
            correction_rule=topological_rule,
            keyword_trigger=keyword_trigger,
            category="TCL",
            severity="directive",
        )

    def get_active_tcl(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_logs WHERE category = 'TCL' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def commit_tgl(
        self,
        error_description: str,
        topological_case: str,
        guard_rule: str,
        keyword_trigger: str,
        context_tags: list[str],
        severity: str = "error",
    ) -> int:
        return self.commit_memory(
            context_tags=context_tags,
            action_taken=f"[TGL] 발생: {error_description}",
            result=f"위상 케이스: {topological_case}",
            correction_rule=guard_rule,
            keyword_trigger=keyword_trigger,
            category="TGL",
            severity=severity,
        )

    def get_active_tgl(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_logs WHERE category = 'TGL' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def preflight(self, query: str, limit: int = 5) -> dict:
        results = self.search(query, limit=limit * 3)
        return {
            "tcl_hits": [r for r in results if r["category"] == "TCL"],
            "tgl_hits": [r for r in results if r["category"] == "TGL"],
            "general_hits": [r for r in results if r["category"] not in ("TCL", "TGL")],
        }

    def get_recent(self, n: int = 10, category: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM memory_logs WHERE category = ? ORDER BY id DESC LIMIT ?",
                    (category, n),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_logs ORDER BY id DESC LIMIT ?", (n,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_correction_rules(self, tags: list[str]) -> list[dict]:
        query = " OR ".join(tags)
        results = self.search(query, limit=20)
        return [r for r in results if r.get("correction_rule")]

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memory_logs").fetchone()[0]
            by_category = conn.execute(
                "SELECT category, COUNT(*) as cnt FROM memory_logs GROUP BY category ORDER BY cnt DESC"
            ).fetchall()
            by_severity = conn.execute(
                "SELECT severity, COUNT(*) as cnt FROM memory_logs GROUP BY severity ORDER BY cnt DESC"
            ).fetchall()
            return {
                "total_records": total,
                "by_category": {r["category"]: r["cnt"] for r in by_category},
                "by_severity": {r["severity"]: r["cnt"] for r in by_severity},
            }

    def export_json(self) -> str:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM memory_logs ORDER BY id").fetchall()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys
    db = MemoryDB()
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python fts5_memory.py search <query>")
        print("  python fts5_memory.py preflight <query>")
        print("  python fts5_memory.py recent [n] [category]")
        print("  python fts5_memory.py tcl")
        print("  python fts5_memory.py tgl")
        print("  python fts5_memory.py stats")
        print("  python fts5_memory.py export")
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "search" and len(sys.argv) >= 3:
        query = " ".join(sys.argv[2:])
        results = db.search(query)
        for r in results:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print("---")
        if not results:
            print("(검색 결과 없음)")
    elif cmd == "preflight" and len(sys.argv) >= 3:
        query = " ".join(sys.argv[2:])
        pf = db.preflight(query)
        for label, hits in pf.items():
            if hits:
                print(f"\n=== {label} ===")
                for r in hits:
                    print(f"  [{r['severity']}] {r['correction_rule']}")
                    print(f"    triggers: {r['keyword_trigger']}")
    elif cmd == "recent":
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        cat = sys.argv[3] if len(sys.argv) >= 4 else None
        for r in db.get_recent(n, cat):
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print("---")
    elif cmd == "tcl":
        for r in db.get_active_tcl():
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print("---")
    elif cmd == "tgl":
        for r in db.get_active_tgl():
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print("---")
    elif cmd == "stats":
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    elif cmd == "export":
        print(db.export_json())
    else:
        print(f"Unknown command: {cmd}")
