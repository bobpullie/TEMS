"""
TEMS Rule Decay — 30일/90일 자동 cold/archive 전환 (Phase 3C)
==============================================================
장기간 발동되지 않은 규칙을 cold → archive 로 전환하여 rule 공간을
정리한다. preflight 가 archive 규칙을 자동 제외하므로, 이 스크립트만
주기적으로 실행하면 된다.

규칙:
- last_fired 가 NULL 이면 created_at 을 기준으로 사용
- 30일 이상 미발동 + status='warm' → status='cold'
- 90일 이상 미발동 + status in ('warm','cold') → status='archive'
- 한 번 archive 된 규칙은 이 스크립트가 되살리지 않음 — 운영자가 수동 fallback

실행:
  python memory/decay.py              # apply (기본)
  python memory/decay.py --dry-run    # 전환 없이 시뮬레이션만
  python memory/decay.py --json       # 결과 JSON

Cron 권장:
  Windows Task Scheduler: 매일 09:00 실행
  Linux cron:             0 9 * * *   python /.../memory/decay.py
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = Path(__file__).parent
DB_PATH = MEMORY_DIR / "error_logs.db"
DIAG_PATH = MEMORY_DIR / "tems_diagnostics.jsonl"

COLD_DAYS = 30
ARCHIVE_DAYS = 90


def _log_diagnostic(event: str, exc: Exception) -> None:
    import traceback
    try:
        with open(DIAG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "event": event,
                "exc_type": type(exc).__name__,
                "exc_msg": str(exc)[:300],
                "traceback": traceback.format_exc()[-800:],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def parse_ts(s) -> datetime | None:
    """ISO 또는 sqlite datetime('now') 포맷 호환 파싱."""
    if not s:
        return None
    s = str(s)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def effective_last_activity(row: dict) -> datetime | None:
    """last_fired > last_activated > memory_logs.timestamp > status_changed_at 순으로 대체.

    rule_health.created_at 은 백필 시점을 반영하므로 신뢰할 수 없다.
    실제 규칙 등록일은 memory_logs.timestamp 에 보존된다.
    """
    for key in ("last_fired", "last_activated", "log_timestamp", "status_changed_at"):
        ts = parse_ts(row.get(key))
        if ts:
            return ts
    return None


def classify_transition(row: dict, now: datetime) -> tuple[str | None, int, str]:
    """(new_status or None, age_days, reason). new_status=None 이면 전환 불필요."""
    current = (row.get("status") or "warm").lower()
    last = effective_last_activity(row)
    if not last:
        return None, -1, "no activity timestamps available"

    age = (now - last).days
    fire_count = int(row.get("fire_count") or 0)

    if age < COLD_DAYS:
        return None, age, "within warm window"

    # 90일 이상이면 archive (이미 archive 아니면)
    if age >= ARCHIVE_DAYS and current != "archive":
        return "archive", age, f"age {age}d >= {ARCHIVE_DAYS}d, fire_count={fire_count}"

    # 30일 이상이면 cold (warm 일 때만)
    if age >= COLD_DAYS and current == "warm":
        return "cold", age, f"age {age}d >= {COLD_DAYS}d, fire_count={fire_count}"

    return None, age, f"no transition needed (current={current})"


def apply_decay(dry_run: bool = False) -> dict:
    if not DB_PATH.exists():
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}

    now = datetime.now()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT rh.rule_id, rh.status, rh.fire_count, rh.last_fired,
                   rh.last_activated, rh.status_changed_at,
                   m.timestamp AS log_timestamp,
                   m.category, rh.classification,
                   substr(m.correction_rule, 1, 80) AS body_preview
            FROM rule_health rh
            LEFT JOIN memory_logs m ON m.id = rh.rule_id
        """).fetchall()
    except Exception as e:
        _log_diagnostic("decay_query_failure", e)
        return {"ok": False, "error": f"DB query failed: {e}"}

    transitions = []
    for r in rows:
        row_dict = dict(r)
        new_status, age, reason = classify_transition(row_dict, now)
        if new_status and new_status != (row_dict.get("status") or "warm"):
            transitions.append({
                "rule_id": row_dict["rule_id"],
                "category": row_dict.get("category"),
                "classification": row_dict.get("classification"),
                "from": row_dict.get("status") or "warm",
                "to": new_status,
                "age_days": age,
                "reason": reason,
                "body_preview": row_dict.get("body_preview", ""),
            })

    if not dry_run and transitions:
        try:
            now_str = now.isoformat()
            for t in transitions:
                conn.execute("""
                    UPDATE rule_health
                    SET status = ?, status_changed_at = ?
                    WHERE rule_id = ?
                """, (t["to"], now_str, t["rule_id"]))
            conn.commit()
        except Exception as e:
            _log_diagnostic("decay_update_failure", e)
            conn.close()
            return {"ok": False, "error": f"update failed: {e}", "transitions": transitions}

    conn.close()

    summary = {
        "ok": True,
        "dry_run": dry_run,
        "now": now.isoformat(),
        "total_rules": len(rows),
        "transitions": len(transitions),
        "to_cold": sum(1 for t in transitions if t["to"] == "cold"),
        "to_archive": sum(1 for t in transitions if t["to"] == "archive"),
        "details": transitions,
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description="TEMS Rule Decay — cold/archive 자동 전환")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 시뮬레이션만")
    parser.add_argument("--json", action="store_true", help="결과 JSON 출력")
    args = parser.parse_args()

    result = apply_decay(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if not result.get("ok"):
        print(f"[decay] FAILED: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    mode = "DRY-RUN" if result["dry_run"] else "APPLIED"
    print(f"[decay] {mode}: total={result['total_rules']}, transitions={result['transitions']} "
          f"(cold+{result['to_cold']}, archive+{result['to_archive']})")
    for t in result["details"]:
        print(f"  #{t['rule_id']} [{t['category']}/{t['classification'] or '-'}] "
              f"{t['from']}→{t['to']} ({t['age_days']}d) — {t['body_preview']}")


if __name__ == "__main__":
    main()
