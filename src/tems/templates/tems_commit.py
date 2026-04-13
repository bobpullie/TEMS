"""
TEMS 규칙 등록 CLI — tems 패키지 API 사용
"""

import argparse
import sqlite3
import os
import sys
import json
from datetime import datetime
from pathlib import Path

from tems.fts5_memory import MemoryDB


def find_agent_root(start: Path) -> Path:
    """상위 순회하며 .claude/tems_agent_id 찾기"""
    cur = start.resolve()
    while cur != cur.parent:
        marker = cur / ".claude" / "tems_agent_id"
        if marker.exists():
            return cur
        cur = cur.parent
    raise FileNotFoundError("tems_agent_id not found from " + str(start))


AGENT_ROOT = find_agent_root(Path(__file__).parent)
AGENT_ID = (AGENT_ROOT / ".claude" / "tems_agent_id").read_text(encoding="utf-8").strip()
DB_PATH = str(AGENT_ROOT / "memory" / "error_logs.db")
QMD_RULES_DIR = AGENT_ROOT / "memory" / "qmd_rules"


def _check_duplicates(db: MemoryDB, category: str, rule: str, triggers: str) -> dict | None:
    """중복/유사 규칙 검사. 문제 있으면 error dict 반환, 없으면 None."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT id, correction_rule FROM memory_logs WHERE correction_rule = ?", (rule,)
        ).fetchone()
        if row:
            return {"ok": False, "error": f"Duplicate rule (id={row['id']}): {row['correction_rule'][:60]}..."}

        rows = conn.execute(
            "SELECT id, keyword_trigger, correction_rule FROM memory_logs WHERE category = ?", (category,)
        ).fetchall()
        new_kw = set(triggers.split())
        for r in rows:
            existing_kw = set(r["keyword_trigger"].split())
            if existing_kw and new_kw:
                overlap = len(existing_kw & new_kw) / max(len(existing_kw), len(new_kw))
                if overlap > 0.8:
                    return {"ok": False, "error": f"Similar rule exists (id={r['id']}, overlap={overlap:.0%}): {r['correction_rule'][:60]}..."}
    return None


def commit_rule(category: str, rule: str, triggers: str, tags: str, source: str = "agent-auto") -> dict:
    if not os.path.exists(DB_PATH):
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}

    db = MemoryDB(db_path=DB_PATH)

    # 중복 검사
    dup = _check_duplicates(db, category, rule, triggers)
    if dup:
        return dup

    # MemoryDB API로 커밋
    full_tags = [t.strip() for t in tags.split(",") if t.strip()] + [f"source:{source}"]

    if category == "TCL":
        rule_id = db.commit_tcl(
            original_instruction=rule,
            topological_rule=rule,
            keyword_trigger=triggers,
            context_tags=full_tags,
        )
    elif category == "TGL":
        rule_id = db.commit_tgl(
            error_description=rule,
            topological_case=rule,
            guard_rule=rule,
            keyword_trigger=triggers,
            context_tags=full_tags,
        )
    else:
        rule_id = db.commit_memory(
            context_tags=full_tags,
            action_taken="auto-registered",
            result="pending",
            correction_rule=rule,
            keyword_trigger=triggers,
            category=category,
        )

    # rule_health 초기화
    with db._conn() as conn:
        try:
            conn.execute(
                "INSERT INTO rule_health (rule_id, ths_score, status) VALUES (?, 0.5, 'warm')",
                (rule_id,),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # 이미 존재

    # QMD 자동 동기화
    try:
        from tems.tems_engine import sync_single_rule_to_qmd
        sync_single_rule_to_qmd(rule_id, db=db, qmd_rules_dir=QMD_RULES_DIR)
    except Exception:
        pass

    return {"ok": True, "rule_id": rule_id, "category": category, "rule": rule[:80]}


def main():
    parser = argparse.ArgumentParser(description="TEMS 규칙 등록 CLI")
    parser.add_argument("--type", required=True, choices=["TCL", "TGL"])
    parser.add_argument("--rule", required=True)
    parser.add_argument("--triggers", required=True)
    parser.add_argument("--tags", default="")
    parser.add_argument("--source", default="agent-auto")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = commit_rule(args.type, args.rule, args.triggers, args.tags, args.source)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["ok"]:
            print(f"[TEMS] {args.type} #{result['rule_id']} registered: {result['rule']}")
        else:
            print(f"[TEMS] FAILED: {result['error']}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()
