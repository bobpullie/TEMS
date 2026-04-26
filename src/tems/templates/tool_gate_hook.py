"""
TEMS Tool Gate Hook — PreToolUse 사전 차단/경고 (Phase 3A)
=============================================================
Claude Code의 PreToolUse hook. 도구 호출 직전에 발동되어:
1. DB에서 TGL-T(Tool Action) + tool_pattern 슬롯을 가진 활성 규칙을 로드
2. tool_name + tool_input 요약문을 대상으로 각 tool_pattern 정규식 매칭
3. 매칭 시:
   - severity=critical → JSON decision=deny 로 도구 호출 차단
   - severity=warning  → stdout 경고만 출력, 호출은 허용
4. 동시에 active_guards.json 에 발동 기록 (compliance tracker 입력)

설계 원칙:
- 사용자가 명시적으로 권한을 승인한 도구 호출을 "차단"하는 것은 무거운 결정.
  → 기본 severity는 warning. critical은 운영자가 명시 지시한 규칙만.
- 정규식 매칭은 tool_name + (command|file_path|old_string|new_string)의
  첫 2kB만 대상으로 한다. 긴 파일 내용 전체를 스캔하지 않음.
- 자기 자신(tems_commit.py, memory/*.py)을 호출하는 명령은 무시한다 (self-trigger 루프 방지).

stdin:  { "tool_name": "...", "tool_input": {...}, "session_id": "...", ... }
stdout:
  - 차단 시: { "hookSpecificOutput": { "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..." } }
  - 경고 시: <tgl-tool-alert>...</tgl-tool-alert> 텍스트 (에이전트 컨텍스트에 주입)
  - 무매칭 시: 아무것도 출력하지 않음
"""

import sys
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).parent
DB_PATH = MEMORY_DIR / "error_logs.db"
ACTIVE_GUARDS_PATH = MEMORY_DIR / "active_guards.json"
DIAG_PATH = MEMORY_DIR / "tems_diagnostics.jsonl"

# 매칭 대상 payload 크기 상한 — 대용량 파일 내용은 읽지 않음
MAX_PAYLOAD_LEN = 2000

# 자기 자신(TEMS 인프라) 호출은 매칭 제외 — self-trigger 루프 방지
SELF_INVOCATION_MARKERS = [
    "memory/preflight_hook.py",
    "memory/tool_failure_hook.py",
    "memory/tool_gate_hook.py",
    "memory/tems_commit.py",
    "memory/retrospective_hook.py",
    "memory/pattern_detector.py",
    "memory/decay.py",
    "memory/compliance_tracker.py",
    "memory/sdc_commit.py",
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


def parse_tags(context_tags: str) -> dict:
    """context_tags 에서 slot:value 패턴을 dict로 파싱."""
    out = {}
    for part in (context_tags or "").split(","):
        part = part.strip()
        if ":" in part:
            k, _, v = part.partition(":")
            out[k.strip()] = v.strip()
    return out


def load_active_tgl_t_rules() -> list[dict]:
    """classification=TGL-T 이면서 tool_pattern 슬롯이 있는 규칙 로드."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT m.id, m.correction_rule, m.context_tags, m.severity, m.summary,
                   rh.classification, rh.status
            FROM memory_logs m
            LEFT JOIN rule_health rh ON rh.rule_id = m.id
            WHERE m.category = 'TGL'
              AND m.context_tags LIKE '%tool_pattern:%'
              AND (rh.status IS NULL OR rh.status != 'archive')
        """).fetchall()
        conn.close()
    except Exception as e:
        _log_diagnostic("tool_gate_db_read_failure", e)
        return []

    rules = []
    for r in rows:
        tags = parse_tags(r["context_tags"])
        classification = tags.get("classification", r["classification"] or "")
        if classification != "TGL-T":
            continue
        pattern = tags.get("tool_pattern", "")
        if not pattern:
            continue
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            _log_diagnostic(f"tool_gate_regex_compile_failure_rule{r['id']}", e)
            continue
        rules.append({
            "id": r["id"],
            "regex": regex,
            "pattern_raw": pattern,
            "severity": (r["severity"] or "info").lower(),
            "classification": classification,
            "rule_body": r["correction_rule"] or r["summary"] or "",
        })
    return rules


def build_match_target(tool_name: str, tool_input: dict) -> str:
    """tool_name + tool_input 에서 매칭 가능한 문자열 페이로드 생성.

    Phase 3 P1-b 패치: Windows 경로(\\) 를 forward slash 로 정규화하여 정규식
    작성자가 `/` 한 가지만 고려하면 되도록 한다. tool_pattern 을 쓰는 TGL-T
    규칙이 `memory[\\/\\\\]` 같은 혼합 패턴을 쓰지 않아도 되도록 보장.
    """
    parts = [tool_name]
    if not isinstance(tool_input, dict):
        return tool_name
    for key in ("command", "file_path", "path", "url", "pattern", "old_string", "new_string"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            parts.append(f"{key}={v[:MAX_PAYLOAD_LEN // 4]}")
    payload = " | ".join(parts)
    payload = payload.replace("\\", "/")
    return payload[:MAX_PAYLOAD_LEN]


def is_self_invocation(tool_name: str, tool_input: dict) -> bool:
    """Bash 명령어가 TEMS hook/CLI 를 직접 실행하는 경우에만 self-invocation.

    Edit/Write 의 file_path 가 memory/*.py 인 경우는 '규칙이 감시해야 할 대상' 이므로
    self-invocation 으로 보지 않는다. (규칙이 없으면 어차피 매칭 0이라 부하 걱정 없음.)
    """
    if tool_name != "Bash":
        return False
    cmd = str((tool_input or {}).get("command", "")).lower().replace("\\", "/")
    if not cmd:
        return False
    return any(marker in cmd for marker in SELF_INVOCATION_MARKERS)


def record_active_guard(rule_id: int, severity: str, classification: str,
                         tool_pattern: str = "", failure_signature: str = "") -> None:
    """매칭된 guard를 active_guards.json 에 기록. compliance_tracker 가 차후 참조.

    Phase 3 P1-a 패치: rule_id 기준 dedup. 이미 active 인 guard 는 remaining_checks
    리셋만 수행 (같은 rule이 preflight+tool_gate 양쪽에서 같은 세션에 fire 되는 경우 대비).

    Phase 3 P1-a-follow 패치: had_violation=True 인 guard 는 window 리셋 금지.
    그렇지 않으면 사용자가 위반 → 같은 규칙이 재발동 → window 초기화되어 compliance_tracker
    가 위반을 집계할 기회를 잃는 우회 경로가 생긴다. had_violation 자체도 절대 덮어쓰지 않음.
    """
    now = datetime.now().isoformat()
    try:
        if ACTIVE_GUARDS_PATH.exists():
            data = json.loads(ACTIVE_GUARDS_PATH.read_text(encoding="utf-8"))
        else:
            data = {"guards": []}

        guards = data.setdefault("guards", [])
        existing = None
        for g in guards:
            if g.get("rule_id") == rule_id:
                existing = g
                break

        if existing is not None:
            # 이미 활성 — 재발동. fired_at 은 갱신하지만 had_violation 은 보존하고,
            # 위반 이력이 있는 guard 는 window 리셋을 건너뛴다.
            existing["fired_at"] = now
            if not existing.get("had_violation"):
                existing["remaining_checks"] = 5
            if severity and not existing.get("severity"):
                existing["severity"] = severity
            if classification and not existing.get("classification"):
                existing["classification"] = classification
            if tool_pattern and not existing.get("tool_pattern"):
                existing["tool_pattern"] = tool_pattern
            if failure_signature and not existing.get("failure_signature"):
                existing["failure_signature"] = failure_signature
        else:
            guards.append({
                "rule_id": rule_id,
                "classification": classification,
                "severity": severity,
                "tool_pattern": tool_pattern,
                "failure_signature": failure_signature,
                "fired_at": now,
                "source": "tool_gate",
                "remaining_checks": 5,
            })

        ACTIVE_GUARDS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        _log_diagnostic("tool_gate_active_guard_write_failure", e)


# SDC Auto-Dispatch 트리거: git 쓰기 명령 집합
_SDC_GIT_WRITE_PATTERN = re.compile(
    r"\bgit\s+(commit|push|merge|rebase|cherry-pick|revert)\b",
    re.IGNORECASE,
)


def check_sdc_gate(tool_name: str, tool_input: dict, active_guards_data: dict) -> "str | None":
    """SDC Auto-Dispatch Check (TCL #120) — git 쓰기 명령 트리거 시 brief 제출 여부 검사.

    Returns:
        매칭 + brief 미제출 시 <sdc-gate-alert>...</sdc-gate-alert> 문자열.
        그 외 None.
    """
    # Bash 도구가 아니면 패스
    if tool_name != "Bash":
        return None

    cmd = str((tool_input or {}).get("command", ""))
    if not cmd:
        return None

    # TEMS 자기 자신 호출은 매칭 제외 — self-trigger 루프 방지
    if is_self_invocation(tool_name, tool_input):
        return None

    # git 쓰기 명령 매칭 검사
    m = _SDC_GIT_WRITE_PATTERN.search(cmd)
    if not m:
        return None

    verb = m.group(1).lower()

    # brief 제출 완료 여부 확인 — True 이면 gate clear
    if active_guards_data.get("sdc_brief_submitted") is True:
        return None

    return (
        "<sdc-gate-alert>\n"
        f"SDC Auto-Dispatch Check (TCL #120) 경고: git {verb} 호출 감지됨.\n"
        "현재 세션에 SDC brief 제출 기록 없음 (sdc_brief_submitted=false).\n"
        "권장 절차: 3-question gate (Q1 Invariance / Q2 Overhead / Q3 Reversibility) verbalize"
        " → 판정(KEEP/DELEGATE/STAGING) 명시 → 진행.\n"
        "STAGING 판정이면 git add 까지만 본체 허용, commit/push 는 brief 제출 후.\n"
        "참조: .claude/skills/SDC.md § Auto-Dispatch Check\n"
        "</sdc-gate-alert>"
    )


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        data = json.loads(raw)
    except Exception as e:
        _log_diagnostic("tool_gate_stdin_parse_failure", e)
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}

    if not tool_name:
        sys.exit(0)

    # 자기 자신(TEMS 인프라) Bash 실행은 매칭 제외 — self-trigger 방지
    if is_self_invocation(tool_name, tool_input):
        sys.exit(0)

    target = build_match_target(tool_name, tool_input)

    rules = load_active_tgl_t_rules()

    blocking_hits = []
    warning_hits = []

    for rule in rules:
        if rule["regex"].search(target):
            if rule["severity"] == "critical":
                blocking_hits.append(rule)
            else:
                warning_hits.append(rule)

    # 차단 우선
    if blocking_hits:
        rule = blocking_hits[0]
        record_active_guard(
            rule["id"], rule["severity"], rule["classification"],
            tool_pattern=rule.get("pattern_raw", ""),
        )
        reason_lines = [
            f"TGL #{rule['id']} ({rule['classification']}) — 도구 호출 차단",
            f"패턴: {rule['pattern_raw']}",
            rule["rule_body"][:400],
            "이 차단이 잘못되었다면 운영자 확인 후 규칙을 조정하거나 severity를 warning으로 낮추세요.",
        ]
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "\n".join(reason_lines),
            }
        }
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(0)

    # 경고만 — 호출은 허용
    output_lines = []
    if warning_hits:
        lines = ["<tgl-tool-alert>"]
        for rule in warning_hits[:3]:
            record_active_guard(
                rule["id"], rule["severity"], rule["classification"],
                tool_pattern=rule.get("pattern_raw", ""),
            )
            lines.append(f"  #{rule['id']} ({rule['classification']}) matched tool_pattern: {rule['pattern_raw']}")
            body_snippet = rule["rule_body"].replace("\n", " ")[:200]
            lines.append(f"    ↳ {body_snippet}")
        lines.append("  → 위 가드를 준수하여 호출 진행. 준수 여부는 compliance_tracker 가 추적.")
        lines.append("</tgl-tool-alert>")
        output_lines.append("\n".join(lines))

    # SDC gate 검사 — TGL-T 루프 완료 직후 실행, warning only (deny 승격 금지)
    active_guards_data: dict = {}
    try:
        if ACTIVE_GUARDS_PATH.exists():
            active_guards_data = json.loads(ACTIVE_GUARDS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _log_diagnostic("tool_gate_active_guards_read_failure", e)

    sdc_alert = check_sdc_gate(tool_name, tool_input, active_guards_data)
    if sdc_alert:
        output_lines.append(sdc_alert)

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
