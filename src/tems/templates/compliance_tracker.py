"""
TEMS Compliance Tracker — PostToolUse 준수/위반 자동 측정 (Phase 3B)
=======================================================================
매 PostToolUse 이벤트에서 active_guards.json 을 순회하며:
1. 현재 도구 호출이 활성 guard의 forbidden_action / failure_signature / tool_pattern
   을 위반했는지 검사
2. 위반 시 violation_count++ (DB rule_health)
3. guard window (remaining_checks) 감소
4. window 만료 시 위반 0회 → compliance_count++ (가드가 잘 작동했다는 증거)

설계 원칙:
- "효용도 측정"이 목적. guard를 공격적으로 차단할 의도가 아니므로,
  이 hook은 절대 도구 호출을 차단하지 않는다. 측정 + 기록만 수행.
- compliance 측정은 관찰형이므로 매 PostToolUse 마다 rule_health 를 쓴다.
  부하가 걱정되지만 rule_health 는 작은 테이블이고 SQLite 로컬이므로 OK.
- 에이전트 자기 자신(memory/*.py, tems_commit.py)의 호출은 측정 제외 — self-trigger 방지.

stdin:  { "tool_name": "...", "tool_input": {...}, "tool_response": "...", ... }
stdout: 무출력 (또는 위반 감지 시 <compliance-violation> 알림)
"""

import sys
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent  # v0.4: cwd 비의존
DB_PATH = MEMORY_DIR / "error_logs.db"
ACTIVE_GUARDS_PATH = MEMORY_DIR / "active_guards.json"
DIAG_PATH = MEMORY_DIR / "tems_diagnostics.jsonl"
COMPLIANCE_LOG = MEMORY_DIR / "compliance_events.jsonl"

MAX_PAYLOAD_LEN = 2000
MAX_RESPONSE_SCAN = 3000

# Phase 3 P1-c-follow: stale guard eviction. fired_at 기준 24h 이상 갱신되지 않으면
# observation-only 세션 등에서 window 를 소진하지 못해 무한 잔존. TTL 경과 시 조용히 제거.
# (compliance/violation 모두 귀속 불가 — 판정 보류 상태로 기록만 남기고 드랍)
STALE_GUARD_TTL = timedelta(hours=24)

# 자기 자신 호출은 측정 제외
SELF_INVOCATION_MARKERS = [
    "memory/preflight_hook.py",
    "memory/tool_failure_hook.py",
    "memory/tool_gate_hook.py",
    "memory/tems_commit.py",
    "memory/retrospective_hook.py",
    "memory/pattern_detector.py",
    "memory/compliance_tracker.py",
    "memory/decay.py",
]


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


def build_match_target(tool_name: str, tool_input: dict) -> str:
    """tool_gate_hook.build_match_target 과 동일한 계약. 경로 정규화(\\ → /) 포함."""
    parts = [tool_name]
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "url", "pattern", "old_string", "new_string"):
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                parts.append(f"{key}={v[:MAX_PAYLOAD_LEN // 4]}")
    payload = " | ".join(parts).replace("\\", "/")
    return payload[:MAX_PAYLOAD_LEN]


def extract_response_text(tool_response) -> str:
    if isinstance(tool_response, dict):
        out = tool_response.get("output") or tool_response.get("stdout") or ""
        if not out:
            try:
                out = json.dumps(tool_response, ensure_ascii=False)
            except Exception:
                out = str(tool_response)
        return str(out)[:MAX_RESPONSE_SCAN]
    return str(tool_response or "")[:MAX_RESPONSE_SCAN]


def is_self_invocation(tool_name: str, tool_input: dict) -> bool:
    """Bash 명령어가 TEMS hook/CLI 를 직접 실행하는 경우만 self-invocation 으로 판정.

    Edit/Write file_path 가 memory/*.py 인 경우는 compliance 측정 대상에 포함한다.
    """
    if tool_name != "Bash":
        return False
    cmd = str((tool_input or {}).get("command", "")).lower().replace("\\", "/")
    if not cmd:
        return False
    return any(marker in cmd for marker in SELF_INVOCATION_MARKERS)


def load_guards() -> dict:
    if not ACTIVE_GUARDS_PATH.exists():
        return {"guards": []}
    try:
        return json.loads(ACTIVE_GUARDS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _log_diagnostic("compliance_guards_read_failure", e)
        return {"guards": []}


def save_guards(data: dict) -> None:
    try:
        ACTIVE_GUARDS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        _log_diagnostic("compliance_guards_write_failure", e)


def update_counts(rule_id: int, field: str) -> None:
    """rule_health.<field>_count++ (field = 'compliance' | 'violation')."""
    if field not in ("compliance", "violation"):
        return
    col = f"{field}_count"
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(f"""
            INSERT INTO rule_health (rule_id, {col}, ths_score, status)
            VALUES (?, 1, 0.5, 'warm')
            ON CONFLICT(rule_id) DO UPDATE SET
                {col} = COALESCE({col}, 0) + 1
        """, (rule_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        _log_diagnostic("compliance_update_counts_failure", e)


def log_event(event_type: str, rule_id: int, detail: str) -> None:
    try:
        with open(COMPLIANCE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "event": event_type,
                "rule_id": rule_id,
                "detail": detail[:300],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def extract_forbidden_text(rule_body: str) -> str:
    """correction_rule 본문에서 'FORBIDDEN: ...' 이후 한 줄 추출."""
    if not rule_body:
        return ""
    m = re.search(r"FORBIDDEN[:\s]+([^\n]{0,400})", rule_body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def load_rule_bodies(rule_ids: list[int]) -> dict:
    """rule_id → correction_rule 매핑."""
    if not rule_ids:
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        placeholders = ",".join(["?"] * len(rule_ids))
        rows = conn.execute(
            f"SELECT id, correction_rule FROM memory_logs WHERE id IN ({placeholders})",
            rule_ids,
        ).fetchall()
        conn.close()
        return {r[0]: r[1] or "" for r in rows}
    except Exception as e:
        _log_diagnostic("compliance_rule_body_read_failure", e)
        return {}


# FORBIDDEN 휴리스틱은 공격적 — 구조화된 슬롯이 전혀 없는 guard 만 fallback 대상.
# Edit/Write/Bash 만 측정. 관찰형(Read/Glob/Grep/Notebook/WebFetch 등)은 제외.
MUTATING_TOOLS = {"Edit", "Write", "Bash", "NotebookEdit"}

# FORBIDDEN 토큰에서 제거할 noise (도메인 공통 + 한국어 금칙어 어휘 + TEMS 인프라 명사)
FORBIDDEN_NOISE_TOKENS = {
    "the", "and", "for", "not", "with", "that", "this", "from", "into",
    "하지", "금지", "절대", "하면", "않는", "없는", "같은", "위한", "대한",
    "memory", "python", "hook", "tool", "rule", "TEMS", "tems",
    "preflight", "preflight_hook", "tool_failure_hook", "tool_gate_hook",
    "compliance_tracker", "retrospective_hook", "pattern_detector",
    "file", "path", "code", "command",
}


def check_violation(guard: dict, tool_name: str, target: str, response: str, rule_body: str) -> tuple[bool, str]:
    """guard 가 이번 도구 호출에서 위반되었는지 판정.

    Returns (violated, reason).

    Phase 3 P0 패치: FORBIDDEN 키워드 휴리스틱은 tool_pattern / failure_signature
    슬롯이 모두 없는 guard 에만 fallback 으로 적용한다. 또한 distinct token
    기준으로 세고, MUTATING_TOOLS 인 경우에만 측정한다. Read/Glob/Grep 등
    관찰형 도구는 FORBIDDEN 휴리스틱 대상에서 제외.
    """
    # 1) tool_pattern 매칭 — 가장 신뢰도 높은 정확한 가드
    tool_pattern = guard.get("tool_pattern", "")
    if tool_pattern:
        try:
            if re.search(tool_pattern, target, re.IGNORECASE):
                return True, f"tool_pattern matched: {tool_pattern[:80]}"
        except re.error:
            pass

    # 2) failure_signature 매칭 — TGL-D/S, response 대상
    fail_sig = guard.get("failure_signature", "")
    if fail_sig and response:
        try:
            if re.search(fail_sig, response, re.IGNORECASE):
                return True, f"failure_signature matched: {fail_sig[:80]}"
        except re.error:
            pass

    # 3) FORBIDDEN 키워드 휴리스틱 — fallback only.
    #    tool_pattern/failure_signature 가 둘 다 없을 때만, 그리고 변형 도구일 때만.
    if not tool_pattern and not fail_sig and tool_name in MUTATING_TOOLS:
        forbidden = extract_forbidden_text(rule_body)
        if forbidden:
            raw_tokens = re.findall(r"[가-힣A-Za-z_\-][가-힣A-Za-z0-9_\-]{2,}", forbidden)
            seen = set()
            distinct_tokens = []
            for t in raw_tokens:
                low = t.lower()
                if low in FORBIDDEN_NOISE_TOKENS:
                    continue
                if low in seen:
                    continue
                seen.add(low)
                distinct_tokens.append(t)
                if len(distinct_tokens) >= 12:
                    break

            target_low = target.lower()
            distinct_hits = [t for t in distinct_tokens if t.lower() in target_low]
            # 의미 있는 signal 이 되려면 distinct 토큰 3+ 가 동시에 target 에 등장
            if len(distinct_hits) >= 3:
                return True, f"FORBIDDEN distinct-token hits={len(distinct_hits)} ({distinct_hits[:4]})"

    return False, ""


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        data = json.loads(raw)
    except Exception as e:
        _log_diagnostic("compliance_stdin_parse_failure", e)
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response", "")

    if not tool_name:
        sys.exit(0)

    # 자기 자신(TEMS 인프라) Bash 실행은 측정 제외
    if is_self_invocation(tool_name, tool_input):
        sys.exit(0)

    target = build_match_target(tool_name, tool_input)

    guards_data = load_guards()
    guards = guards_data.get("guards", [])
    if not guards:
        sys.exit(0)

    # Phase 3 P1-c-follow: TTL 기반 stale guard 제거.
    # active_guards.json 은 observation-only 세션에서 누적될 수 있어 매 호출마다 청소한다.
    now_dt = datetime.now()
    kept_guards = []
    evicted = 0
    for g in guards:
        fired_raw = g.get("fired_at", "")
        if fired_raw:
            try:
                fired_dt = datetime.fromisoformat(fired_raw)
                if now_dt - fired_dt > STALE_GUARD_TTL:
                    evicted += 1
                    log_event(
                        "eviction",
                        g.get("rule_id", 0),
                        f"stale guard dropped (fired_at={fired_raw}, ttl={STALE_GUARD_TTL})",
                    )
                    continue
            except (ValueError, TypeError):
                # fired_at 파싱 실패는 오래된 스키마 — 그냥 통과시켜 이후 만료 사이클에 처리
                pass
        kept_guards.append(g)

    if evicted > 0:
        guards_data["guards"] = kept_guards
        save_guards(guards_data)

    guards = kept_guards
    if not guards:
        sys.exit(0)

    response_text = extract_response_text(tool_response)

    # rule body 로드 (forbidden 추출용)
    rule_ids = [g.get("rule_id") for g in guards if isinstance(g.get("rule_id"), int)]
    rule_bodies = load_rule_bodies(rule_ids)

    violations_reported = []
    surviving_guards = []

    # Phase 3 P1-c: scope-aware decrement.
    # 관찰형 도구(Read/Glob/Grep/Notebook 조회 등)는 guard 를 "만지지 않는다" —
    # remaining_checks 감소 없음, compliance 누적 없음.
    # 변형/실행 도구(Edit/Write/Bash/NotebookEdit)만 guard window 에 카운트.
    is_scope_tick = tool_name in MUTATING_TOOLS

    for g in guards:
        rid = g.get("rule_id")
        if not isinstance(rid, int):
            continue

        rule_body = rule_bodies.get(rid, "")

        # 관찰형 도구는 위반만 탐지 (tool_pattern/failure_signature 가 매칭되면 위반)
        # 하지만 compliance 적립 대상은 아님. 따라서 check_violation 호출은 하되
        # window 감소는 건너뛴다.
        violated, reason = check_violation(g, tool_name, target, response_text, rule_body)

        if violated:
            update_counts(rid, "violation")
            log_event("violation", rid, reason)
            violations_reported.append({"rule_id": rid, "reason": reason})
            g["had_violation"] = True

        if not is_scope_tick:
            # 관찰형: guard 보존, window 건드리지 않음
            surviving_guards.append(g)
            continue

        remaining = int(g.get("remaining_checks", 0)) - 1
        if remaining <= 0:
            # window 만료 — 위반 이력 없으면 compliance++ (scope-aware 틱 기준)
            if not g.get("had_violation"):
                update_counts(rid, "compliance")
                log_event("compliance", rid, f"window closed clean (scope ticks consumed)")
            continue

        g["remaining_checks"] = remaining
        surviving_guards.append(g)

    guards_data["guards"] = surviving_guards
    save_guards(guards_data)

    if violations_reported:
        lines = ["<compliance-violation>"]
        for v in violations_reported[:3]:
            lines.append(f"  TGL #{v['rule_id']}: {v['reason']}")
        lines.append("  → rule_health.violation_count 증가. 반복 시 efficacy_score 재평가.")
        lines.append("</compliance-violation>")
        print("\n".join(lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
