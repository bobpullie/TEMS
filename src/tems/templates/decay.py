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

MEMORY_DIR = Path(__file__).resolve().parent  # v0.4: cwd 비의존
DB_PATH = MEMORY_DIR / "error_logs.db"
DIAG_PATH = MEMORY_DIR / "tems_diagnostics.jsonl"

COLD_DAYS = 30
ARCHIVE_DAYS = 90

# THS recomputation (관련성 게이트 보조 — preflight final_score 의 0.4 가중치)
THS_FIRE_FULL_CONFIDENCE = 10  # fire_count 가 이 값에 도달하면 confidence=1.0
THS_NEUTRAL = 0.5              # 신호 없음 시 default


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


# S60 compliance reform — active 인용 신호와 passive 미위반 신호의 가중치
THS_ACTIVE_WEIGHT = 1.0       # LLM 이 응답에 명시 인용 = 강한 적중 신호
THS_PASSIVE_WEIGHT = 0.3      # 위반 시그니처 미발동 = 약한 적중 신호
PASSIVE_ONLY_CEILING = 0.70   # active=0 + violation=0 인 룰의 utility 점근 상한
PASSIVE_RAMP_HALF = 5         # 점근의 half-life — passive=5 일 때 ramp=0.5


def compute_ths(
    fire_count: int,
    compliance_count: int,
    violation_count: int,
    active_compliance_count: int = 0,
) -> float:
    """fire_count + (active 인용 / passive 미위반 / violation) 비율로 ths_score 산출.

    공식 (S60):
      active > 0:
        weighted_c = active * 1.0 + passive * 0.3
        utility    = weighted_c / (weighted_c + violation)            # 1.0 도달 가능
      active == 0, violation == 0:
        # passive-only — 도구 미사용으로 발생하는 유령 신호. 점근 ceiling 적용.
        ramp    = passive / (passive + 5)                             # 0..1
        utility = 0.5 + (0.7 - 0.5) * ramp                            # 0.5..0.7
      active == 0, violation > 0:
        weighted_c = passive * 0.3
        utility    = weighted_c / (weighted_c + violation)            # passive 작으면 0 근접

      confidence = min(1.0, fire_count / 10)
      ths        = 0.5 + (utility - 0.5) * confidence                 # [0, 1] clamp

    핵심 변화 (vs S58 공식):
      - passive 만 누적된 룰은 utility ≤ 0.7 → ths ≤ 0.7 → 게이트 (0.7) 미통과 시 차단
      - active 인용이 누적되면 utility 1.0 도달 가능 → ths 1.0 도달
      - 결과: 유령 적중 룰 자연 차단, 진짜 적중 룰 우선 부상

    backward-compat: active_compliance_count 미지정 시 0 (기존 호출자 안전).
    """
    active = max(0, active_compliance_count or 0)
    passive = max(0, compliance_count or 0)
    violation = max(0, violation_count or 0)

    if active > 0:
        weighted_c = active * THS_ACTIVE_WEIGHT + passive * THS_PASSIVE_WEIGHT
        cv_total = weighted_c + violation
        utility = weighted_c / cv_total if cv_total > 0 else THS_NEUTRAL
    elif violation > 0:
        weighted_c = passive * THS_PASSIVE_WEIGHT
        utility = weighted_c / (weighted_c + violation)
    elif passive > 0:
        # passive-only, no violation — 점근 ceiling
        ramp = passive / (passive + PASSIVE_RAMP_HALF)
        utility = THS_NEUTRAL + (PASSIVE_ONLY_CEILING - THS_NEUTRAL) * ramp
    else:
        # 신호 없음
        utility = THS_NEUTRAL

    fc = max(0, fire_count or 0)
    confidence = min(1.0, fc / THS_FIRE_FULL_CONFIDENCE)

    ths = THS_NEUTRAL + (utility - THS_NEUTRAL) * confidence
    return max(0.0, min(1.0, ths))


def recompute_ths_scores(dry_run: bool = False) -> dict:
    """rule_health.ths_score 를 fire_count + compliance/violation 비율로 재계산.

    compliance_tracker 가 INSERT 시 0.5 로 초기화 후 갱신하지 않아 모든 룰이
    default 0.5 에 묶여있던 문제 (S60 발견) 를 해소한다. 결과적으로 preflight
    의 THS_WEIGHT(0.4) 가 차별화 신호를 갖게 되어 generic-keyword noise 룰이
    specific-keyword 적중 룰을 BM25 단일 신호로 이기던 현상을 막는다.
    """
    if not DB_PATH.exists():
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_health)").fetchall()}
        required = {"rule_id", "ths_score"}
        missing = required - cols
        if missing:
            conn.close()
            return {"ok": False, "error": f"rule_health missing columns: {missing}"}

        # fire_count / compliance_count / violation_count / active_compliance_count
        # 부재 시 0 으로 보정 (구 스키마 호환)
        select_fields = ["rule_id", "ths_score"]
        for opt in ("fire_count", "compliance_count", "violation_count", "active_compliance_count"):
            select_fields.append(opt if opt in cols else f"0 AS {opt}")
        rows = conn.execute(f"SELECT {', '.join(select_fields)} FROM rule_health").fetchall()
    except Exception as e:
        _log_diagnostic("ths_recompute_query_failure", e)
        return {"ok": False, "error": f"DB query failed: {e}"}

    updates = []
    histogram = {"unchanged": 0, "raised": 0, "lowered": 0}
    for r in rows:
        old = float(r["ths_score"] if r["ths_score"] is not None else THS_NEUTRAL)
        new = compute_ths(
            int(r["fire_count"] or 0),
            int(r["compliance_count"] or 0),
            int(r["violation_count"] or 0),
            int(r["active_compliance_count"] or 0),
        )
        if abs(new - old) < 1e-6:
            histogram["unchanged"] += 1
            continue
        histogram["raised" if new > old else "lowered"] += 1
        updates.append({
            "rule_id": r["rule_id"],
            "from": round(old, 4),
            "to": round(new, 4),
            "fire": int(r["fire_count"] or 0),
            "active": int(r["active_compliance_count"] or 0),
            "compliance": int(r["compliance_count"] or 0),
            "violation": int(r["violation_count"] or 0),
        })

    if not dry_run and updates:
        try:
            for u in updates:
                conn.execute(
                    "UPDATE rule_health SET ths_score = ? WHERE rule_id = ?",
                    (u["to"], u["rule_id"]),
                )
            conn.commit()
        except Exception as e:
            _log_diagnostic("ths_recompute_update_failure", e)
            conn.close()
            return {"ok": False, "error": f"update failed: {e}", "updates": updates}

    conn.close()

    return {
        "ok": True,
        "dry_run": dry_run,
        "total_rules": len(rows),
        "changed": len(updates),
        "histogram": histogram,
        "details": updates,
    }


## S60 --penalize-uncited 임계값
PENALIZE_UNCITED_FIRE_THRESHOLD = 10  # fire_count 가 이 값 이상이면서 active=0 → 페널티 대상


def penalize_uncited(dry_run: bool = False) -> dict:
    """fire_count 누적이 큰데 active_compliance_count = 0 인 룰의 ths 를
    0.5 (neutral) 로 강제 회귀.

    의도: passive compliance 만 누적되어 ths 가 1.0 근처로 부풀려진 유령 적중
    룰을 정기적으로 정리. compliance_tracker Tier 1 + ths active 가중치가
    이미 1차/2차 방어이지만, 이미 누적된 룰 한정으로 안전망 역할.

    실행 권장: 1-2주마다 cron 으로 --recompute-ths 직후 1회 실행.
    """
    if not DB_PATH.exists():
        return {"ok": False, "error": f"DB not found: {DB_PATH}"}

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_health)").fetchall()}
        if "active_compliance_count" not in cols:
            conn.close()
            return {"ok": False, "error": "active_compliance_count column missing — run schema migration"}

        rows = conn.execute("""
            SELECT rule_id, ths_score, fire_count, compliance_count, violation_count,
                   active_compliance_count, status
            FROM rule_health
            WHERE COALESCE(status, 'warm') != 'archive'
        """).fetchall()
    except Exception as e:
        _log_diagnostic("penalize_uncited_query_failure", e)
        return {"ok": False, "error": f"DB query failed: {e}"}

    penalties = []
    for r in rows:
        fire = int(r["fire_count"] or 0)
        active = int(r["active_compliance_count"] or 0)
        violation = int(r["violation_count"] or 0)
        ths = float(r["ths_score"] if r["ths_score"] is not None else THS_NEUTRAL)
        # 페널티 조건: 자주 발화 + LLM 인용 0 + ths 가 neutral 위
        if (
            fire >= PENALIZE_UNCITED_FIRE_THRESHOLD
            and active == 0
            and violation == 0
            and ths > THS_NEUTRAL + 1e-6
        ):
            penalties.append({
                "rule_id": r["rule_id"],
                "from_ths": round(ths, 4),
                "to_ths": THS_NEUTRAL,
                "fire": fire,
                "passive_c": int(r["compliance_count"] or 0),
            })

    if not dry_run and penalties:
        try:
            for p in penalties:
                conn.execute(
                    "UPDATE rule_health SET ths_score = ? WHERE rule_id = ?",
                    (p["to_ths"], p["rule_id"]),
                )
            conn.commit()
        except Exception as e:
            _log_diagnostic("penalize_uncited_update_failure", e)
            conn.close()
            return {"ok": False, "error": f"update failed: {e}", "penalties": penalties}

    conn.close()

    return {
        "ok": True,
        "dry_run": dry_run,
        "total_rules": len(rows),
        "penalized": len(penalties),
        "details": penalties,
    }


def main():
    parser = argparse.ArgumentParser(description="TEMS Rule Decay — cold/archive 자동 전환 + ths_score 재계산")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 시뮬레이션만")
    parser.add_argument("--json", action="store_true", help="결과 JSON 출력")
    parser.add_argument(
        "--recompute-ths",
        action="store_true",
        help="status decay 대신 ths_score 재계산 실행 (preflight 관련성 게이트 보조)",
    )
    parser.add_argument(
        "--penalize-uncited",
        action="store_true",
        help="fire_count >= 10 + active_compliance == 0 + ths > 0.5 룰의 ths 를 0.5 로 강제 회귀 (S60 안전망)",
    )
    args = parser.parse_args()

    if args.penalize_uncited:
        result = penalize_uncited(dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if result.get("ok") else 1)
        if not result.get("ok"):
            print(f"[penalize] FAILED: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        mode = "DRY-RUN" if result["dry_run"] else "APPLIED"
        print(f"[penalize-uncited] {mode}: total={result['total_rules']}, penalized={result['penalized']}")
        for p in result["details"][:30]:
            print(f"  #{p['rule_id']}: ths {p['from_ths']:.3f} → {p['to_ths']:.3f} "
                  f"(fire={p['fire']}, passive_c={p['passive_c']}, active=0)")
        if len(result["details"]) > 30:
            print(f"  ... ({len(result['details']) - 30} more)")
        return

    if args.recompute_ths:
        result = recompute_ths_scores(dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0 if result.get("ok") else 1)
        if not result.get("ok"):
            print(f"[ths] FAILED: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        mode = "DRY-RUN" if result["dry_run"] else "APPLIED"
        h = result["histogram"]
        print(f"[ths] {mode}: total={result['total_rules']}, changed={result['changed']} "
              f"(raised+{h['raised']}, lowered+{h['lowered']}, unchanged={h['unchanged']})")
        for u in result["details"][:20]:
            print(f"  #{u['rule_id']}: {u['from']:.3f} → {u['to']:.3f} "
                  f"(fire={u['fire']}, active={u.get('active', 0)}, "
                  f"c={u['compliance']}, v={u['violation']})")
        if len(result["details"]) > 20:
            print(f"  ... ({len(result['details']) - 20} more)")
        return

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
