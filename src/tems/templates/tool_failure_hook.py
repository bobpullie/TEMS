"""
TEMS Tool Failure Hook — PostToolUse 자동 실패 감지 (Phase 0 T1.2)
====================================================================
Bash 등 도구의 결과에서 실패 시그니처를 감지하여:
1. memory/tool_failures.jsonl 에 영속화 (메타학습 채널)
2. <tool-failure-detected> 블록을 stdout으로 출력 → 에이전트 컨텍스트 즉시 주입

이 hook은 silent fail을 시스템 차원에서 차단하기 위한 자기관찰 메커니즘이다.
S29 표준화 사고 같이 ModuleNotFoundError가 except:pass로 묻히는 패턴을 잡는다.

stdin: { "tool_name": "...", "tool_input": {...}, "tool_response": "..." }
stdout: <tool-failure-detected> 블록 (감지 시) 또는 무출력
"""

import sys
import json
import re
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent  # v0.4: cwd 비의존
LOG_PATH = MEMORY_DIR / "tool_failures.jsonl"

# 실패 시그니처 — 우선순위 높은 순. (regex_pattern, signature_label, severity)
FAILURE_SIGNATURES = [
    (r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]", "module_not_found", "critical"),
    (r"ImportError:\s*([^\n]{0,120})", "import_error", "critical"),
    (r"FileNotFoundError:\s*\[Errno \d+\][^:]*:\s*['\"]?([^'\"]{0,200})['\"]?", "file_not_found", "high"),
    (r"PermissionError:\s*([^\n]{0,120})", "permission_error", "high"),
    (r"sqlite3\.(?:Operational|Integrity|Programming)Error:\s*([^\n]{0,150})", "sqlite_error", "high"),
    (r"Traceback \(most recent call last\)", "python_traceback", "high"),
    (r"SyntaxError:\s*([^\n]{0,120})", "syntax_error", "high"),
    (r"command not found:\s*(\S+)", "shell_command_not_found", "medium"),
    (r"bash:\s*([^:]+):\s*command not found", "shell_command_not_found", "medium"),
    (r"fatal:\s+([^\n]{0,200})", "git_fatal", "medium"),  # git 에러
    (r"npm ERR!\s*([^\n]{0,150})", "npm_error", "medium"),
]

# 무시 — 정상 동작이지만 키워드가 들어가는 케이스
IGNORE_PATTERNS = [
    r"echo\s+['\"]Error:",  # 명시적 에코는 의도된 것
    r"grep .*['\"]error['\"]",  # 에러 grep은 의도된 것
    r"# error",  # 주석 안의 error
]


def is_ignored(tool_input: dict, response: str) -> bool:
    """의도된 키워드 출현은 무시."""
    cmd = str(tool_input.get("command", ""))
    for pat in IGNORE_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return True
    return False


def detect_failures(response: str, max_matches: int = 3) -> list[dict]:
    """tool_response에서 실패 시그니처를 감지."""
    if not response or len(response) < 5:
        return []

    matches = []
    for pattern, label, severity in FAILURE_SIGNATURES:
        for m in re.finditer(pattern, response, re.IGNORECASE):
            detail = m.group(1) if m.groups() else m.group(0)
            matches.append({
                "signature": label,
                "severity": severity,
                "detail": detail.strip()[:200],
            })
            if len(matches) >= max_matches:
                return matches
    return matches


def append_log(record: dict) -> None:
    """jsonl로 영속화. 실패해도 hook은 계속."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def emit_alert(matches: list[dict], tool_name: str, cmd_summary: str) -> None:
    """에이전트 컨텍스트에 즉시 주입할 알림."""
    print("<tool-failure-detected>")
    print(f"  tool: {tool_name}")
    if cmd_summary:
        print(f"  cmd: {cmd_summary[:200]}")
    for m in matches:
        print(f"  [{m['severity']}] {m['signature']}: {m['detail']}")
    print("  → tool_failures.jsonl 에 기록됨. 동일 패턴 N회 반복 시 TGL 등록 검토.")
    print("</tool-failure-detected>")


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        data = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    tool_response = data.get("tool_response", "")

    # tool_response가 dict일 수도 있음 (Claude Code의 다양한 포맷)
    if isinstance(tool_response, dict):
        tool_response = tool_response.get("output", "") or tool_response.get("stdout", "") or json.dumps(tool_response)
    tool_response = str(tool_response)

    if is_ignored(tool_input, tool_response):
        sys.exit(0)

    matches = detect_failures(tool_response)
    if not matches:
        sys.exit(0)

    cmd_summary = str(tool_input.get("command", "") or tool_input.get("file_path", ""))[:200]

    record = {
        "timestamp": datetime.now().isoformat(),
        "tool_name": tool_name,
        "cmd_summary": cmd_summary,
        "matches": matches,
        "response_excerpt": tool_response[-500:] if len(tool_response) > 500 else tool_response,
    }
    append_log(record)
    emit_alert(matches, tool_name, cmd_summary)
    sys.exit(0)


if __name__ == "__main__":
    main()
