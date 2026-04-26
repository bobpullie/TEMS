"""
TEMS — Rebuild DB from QMD markdown rules (md → DB 역방향 파서)
================================================================
memory/qmd_rules/rule_*.md 을 순회하여 DB의 memory_logs + rule_health 를
재구축한다. DB가 .gitignore 대상인 반면 qmd_rules/*.md 가 정규 소스이므로,
신규 클론 또는 DB 손상 시 이 스크립트로 복원한다.

Usage:
  # 에이전트 루트 자동 감지
  python rebuild_from_qmd.py --agent-root "E:/KJI_Portfolio"
  python rebuild_from_qmd.py --agent-root "E:/KJI_Portfolio" --dry-run

  # 수동 경로 지정
  python rebuild_from_qmd.py --db "E:/X/memory/error_logs.db" --qmd-dir "E:/X/memory/qmd_rules"
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# ═══════════════════════════════════════════════════════════
# 파서: frontmatter + 본문 섹션
# ═══════════════════════════════════════════════════════════

def parse_qmd_rule(md_path: Path) -> dict | None:
    """rule_NNNN.md 파일을 dict로 파싱. 실패 시 None."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return None

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.+)", text, re.DOTALL)
    if not m:
        return None

    fm_text = m.group(1)
    body = m.group(2)

    fm = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()

    # 본문 섹션 파싱: **Keywords:** / **Rule:** / **Context:** / **Result:**
    sections = {}
    for key in ["Keywords", "Rule", "Context", "Result"]:
        mm = re.search(
            rf"\*\*{key}:\*\*\s*(.+?)(?=\n\*\*|\Z)",
            body,
            re.DOTALL,
        )
        if mm:
            sections[key.lower()] = mm.group(1).strip()

    try:
        rule_id = int(fm.get("rule_id", 0))
    except ValueError:
        rule_id = 0

    if rule_id <= 0:
        return None

    return {
        "rule_id": rule_id,
        "category": fm.get("category", "general"),
        "context_tags": fm.get("tags", ""),
        "keyword_trigger": sections.get("keywords", fm.get("trigger", "")),
        "correction_rule": sections.get("rule", ""),
        "action_taken": sections.get("context", ""),
        "result": sections.get("result", ""),
        "severity": fm.get("severity", "info"),
    }


# ═══════════════════════════════════════════════════════════
# 에이전트 루트 → 경로 해석
# ═══════════════════════════════════════════════════════════

def resolve_agent_paths(agent_root: Path) -> tuple[Path, Path]:
    """agent_root 에서 DB 경로와 qmd_rules 경로 반환.

    tems_agent_id 마커 존재 여부와 무관하게 memory/ 하위를 사용.
    """
    memory_dir = agent_root / "memory"
    db_path = memory_dir / "error_logs.db"
    qmd_dir = memory_dir / "qmd_rules"
    return db_path, qmd_dir


# ═══════════════════════════════════════════════════════════
# 스키마 유연 INSERT
# ═══════════════════════════════════════════════════════════

def get_memory_logs_columns(conn: sqlite3.Connection) -> set[str]:
    cols = conn.execute("PRAGMA table_info(memory_logs)").fetchall()
    return {c[1] for c in cols}


def insert_rule(conn: sqlite3.Connection, rule: dict, columns: set[str]) -> str:
    """memory_logs 에 rule 삽입. 이미 존재하면 skip.

    컬럼 존재 여부에 따라 schema 차이 흡수.
    """
    rule_id = rule["rule_id"]

    exists = conn.execute(
        "SELECT 1 FROM memory_logs WHERE id = ?", (rule_id,)
    ).fetchone()
    if exists:
        return "skip_exists"

    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    correction = rule.get("correction_rule", "") or ""

    # 기본값 + 필수 컬럼
    row: dict = {
        "id": rule_id,
        "context_tags": rule.get("context_tags", ""),
        "keyword_trigger": rule.get("keyword_trigger", ""),
        "action_taken": rule.get("action_taken", "") or "rebuilt_from_qmd",
        "result": rule.get("result", "") or "pending",
        "correction_rule": correction,
        "category": rule.get("category", "general"),
        "severity": rule.get("severity", "info"),
    }

    # 스키마별 선택 컬럼
    if "timestamp" in columns:
        row["timestamp"] = now_iso
    if "created_at" in columns:
        row["created_at"] = now_iso
    if "summary" in columns:
        row["summary"] = correction[:120]

    # DB 에 실제로 존재하는 컬럼만 필터
    usable = {k: v for k, v in row.items() if k in columns}

    col_list = ", ".join(usable.keys())
    placeholders = ", ".join(["?"] * len(usable))
    sql = f"INSERT INTO memory_logs ({col_list}) VALUES ({placeholders})"
    conn.execute(sql, list(usable.values()))
    return "inserted"


def upsert_rule_health(conn: sqlite3.Connection, rule_id: int):
    """rule_health 기본값 삽입 (존재 시 skip)."""
    # 테이블 존재 여부 확인
    exists_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_health'"
    ).fetchone()
    if not exists_table:
        return

    existing = conn.execute(
        "SELECT 1 FROM rule_health WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    if existing:
        return

    try:
        conn.execute(
            """
            INSERT INTO rule_health (rule_id, ths_score, status)
            VALUES (?, 0.5, 'warm')
            """,
            (rule_id,),
        )
    except sqlite3.OperationalError:
        # 컬럼 세트가 다른 구버전 스키마 — 최소 컬럼만 시도
        try:
            conn.execute(
                "INSERT INTO rule_health (rule_id) VALUES (?)",
                (rule_id,),
            )
        except Exception:
            pass


def rebuild_fts_index(conn: sqlite3.Connection):
    """FTS5 인덱스 재구축."""
    try:
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass


# ═══════════════════════════════════════════════════════════
# 메인 재구축 플로우
# ═══════════════════════════════════════════════════════════

def rebuild(db_path: Path, qmd_dir: Path, dry_run: bool) -> dict:
    if not qmd_dir.exists():
        return {
            "ok": False,
            "error": f"qmd_dir not found: {qmd_dir}",
        }

    rule_files = sorted(qmd_dir.glob("rule_*.md"))
    if not rule_files:
        return {
            "ok": True,
            "parsed": 0,
            "inserted": 0,
            "skipped": 0,
            "failed": 0,
            "message": "no rule files found",
        }

    parsed_rules: list[dict] = []
    failed: list[str] = []

    for rf in rule_files:
        parsed = parse_qmd_rule(rf)
        if parsed is None:
            failed.append(rf.name)
        else:
            parsed_rules.append(parsed)

    result = {
        "ok": True,
        "db_path": str(db_path),
        "qmd_dir": str(qmd_dir),
        "dry_run": dry_run,
        "parsed": len(parsed_rules),
        "failed": len(failed),
        "failed_files": failed,
        "inserted": 0,
        "skipped_existing": 0,
        "rules_preview": [],
    }

    # dry-run: 파싱 결과만 보여줌
    if dry_run:
        for r in parsed_rules:
            result["rules_preview"].append({
                "rule_id": r["rule_id"],
                "category": r["category"],
                "keyword_trigger": r["keyword_trigger"][:80],
                "correction_rule": (r["correction_rule"] or "")[:80],
            })
        return result

    # 실제 insert
    if not db_path.exists():
        return {
            **result,
            "ok": False,
            "error": f"db_path not found: {db_path} (run tems_scaffold first)",
        }

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        columns = get_memory_logs_columns(conn)
        if not columns:
            return {
                **result,
                "ok": False,
                "error": "memory_logs table missing",
            }

        inserted = 0
        skipped = 0
        for rule in parsed_rules:
            status = insert_rule(conn, rule, columns)
            if status == "inserted":
                inserted += 1
                upsert_rule_health(conn, rule["rule_id"])
            elif status == "skip_exists":
                skipped += 1

        conn.commit()
        rebuild_fts_index(conn)
        conn.commit()

        result["inserted"] = inserted
        result["skipped_existing"] = skipped
    finally:
        conn.close()

    return result


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Rebuild TEMS DB from qmd_rules/*.md (reverse sync)"
    )
    parser.add_argument(
        "--agent-root",
        type=str,
        help="에이전트 루트 (e.g. E:/KJI_Portfolio). memory/error_logs.db + memory/qmd_rules 자동 탐지",
    )
    parser.add_argument("--db", type=str, help="DB 경로 수동 지정")
    parser.add_argument("--qmd-dir", type=str, help="qmd_rules 디렉토리 수동 지정")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파싱 결과만 출력, DB 수정 안 함",
    )

    args = parser.parse_args()

    if args.agent_root:
        agent_root = Path(args.agent_root).resolve()
        db_path, qmd_dir = resolve_agent_paths(agent_root)
    elif args.db and args.qmd_dir:
        db_path = Path(args.db).resolve()
        qmd_dir = Path(args.qmd_dir).resolve()
    else:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "--agent-root OR (--db AND --qmd-dir) required",
                },
                ensure_ascii=False,
            )
        )
        sys.exit(2)

    result = rebuild(db_path, qmd_dir, args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
