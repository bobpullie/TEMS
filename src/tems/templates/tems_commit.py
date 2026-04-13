"""
TEMS 규칙 등록 CLI — 범용 템플릿
"""

import argparse
import sqlite3
import os
import sys
import json
from datetime import datetime
from pathlib import Path



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


def commit_rule(category: str, rule: str, triggers: str, tags: str, source: str = "agent-auto") -> dict:
    if not os.path.exists(DB_PATH):
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat()

    cur.execute("SELECT id, correction_rule FROM memory_logs WHERE correction_rule = ?", (rule,))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return {"ok": False, "error": f"Duplicate rule (id={existing[0]}): {existing[1][:60]}..."}

    cur.execute("SELECT id, keyword_trigger, correction_rule FROM memory_logs WHERE category = ?", (category,))
    for row in cur.fetchall():
        existing_kw = set(row[1].split())
        new_kw = set(triggers.split())
        if existing_kw and new_kw:
            overlap = len(existing_kw & new_kw) / max(len(existing_kw), len(new_kw))
            if overlap > 0.8:
                conn.close()
                return {"ok": False, "error": f"Similar rule exists (id={row[0]}, overlap={overlap:.0%}): {row[2][:60]}..."}

    # context_tags에 source 정보를 포함
    full_tags = f"{tags},source:{source}" if tags else f"source:{source}"

    cur.execute("""
        INSERT INTO memory_logs (timestamp, category, context_tags, keyword_trigger, correction_rule, action_taken, result, severity, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, category, full_tags, triggers, rule, "auto-registered", "pending", "info", rule[:120]))
    rule_id = cur.lastrowid

    cur.execute("""
        INSERT INTO rule_health (rule_id, ths_score, status)
        VALUES (?, 0.5, 'warm')
    """, (rule_id,))

    conn.commit()
    conn.close()

    # QMD 자동 동기화 — 새 규칙 파일 생성
    try:
        from tems.tems_engine import sync_single_rule_to_qmd
        from tems.fts5_memory import MemoryDB
        db = MemoryDB(db_path=DB_PATH)
        sync_single_rule_to_qmd(rule_id, db=db, qmd_rules_dir=QMD_RULES_DIR)
    except Exception:
        pass  # QMD 동기화 실패 시 규칙 등록은 유지

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
