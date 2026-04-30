"""
SDC Brief Submit CLI — memory/sdc_commit.py
============================================
SDC Auto-Dispatch Gate (TCL #120) 에서 3-question gate 판정 결과를 기록하는 helper.

사용법:
    python memory/sdc_commit.py --verdict KEEP --task "S37-smoke" --rationale "Q2: 1줄 편집"
    python memory/sdc_commit.py --verdict DELEGATE --task "S37-impl" --brief "TEMS 모듈 신설..."
    python memory/sdc_commit.py --reset

판정 결과를 active_guards.json 에 세팅하고, sdc_briefs.jsonl 에 audit log 를 남긴다.
tool_gate_hook.py::check_sdc_gate 가 active_guards.json["sdc_brief_submitted"] is True 이면
gate 를 clear 한다 (None 반환).

설계 원칙:
- 외부 PyPI 의존 금지 (self-contained)
- 경로 하드코딩 금지 (Path(__file__).parent 기반)
- silent fail 금지 (JSON 파싱/쓰기 실패 시 stderr + non-zero exit)
- KEEP/DELEGATE/STAGING 모두 sdc_brief_submitted=true (brief 는 판정 선언 행위)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent  # v0.4: cwd 비의존
ACTIVE_GUARDS_PATH = MEMORY_DIR / "active_guards.json"
SDC_LOG_PATH = MEMORY_DIR / "sdc_briefs.jsonl"

VALID_VERDICTS = {"KEEP", "DELEGATE", "STAGING"}


def _load_active_guards() -> dict:
    """active_guards.json 을 읽어 dict 반환. 파일이 없으면 기본 구조 반환."""
    if not ACTIVE_GUARDS_PATH.exists():
        return {"sdc_brief_submitted": False, "guards": []}
    try:
        text = ACTIVE_GUARDS_PATH.read_text(encoding="utf-8")
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"ERROR: active_guards.json JSON 파싱 실패: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: active_guards.json 읽기 실패: {e}", file=sys.stderr)
        sys.exit(1)


def _save_active_guards(data: dict) -> None:
    """active_guards.json 에 data 를 저장. 실패 시 stderr + exit 1."""
    try:
        ACTIVE_GUARDS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"ERROR: active_guards.json 쓰기 실패: {e}", file=sys.stderr)
        sys.exit(1)


def _append_log(entry: dict) -> None:
    """sdc_briefs.jsonl 에 한 줄 append. 실패 시 stderr + exit 1."""
    try:
        with open(SDC_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"ERROR: sdc_briefs.jsonl 쓰기 실패: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_submit(verdict: str, task: str, rationale: str, brief: str, as_json: bool) -> None:
    """판정 결과를 active_guards.json 에 세팅하고 audit log 를 남긴다."""
    if verdict not in VALID_VERDICTS:
        print(
            f"ERROR: 유효하지 않은 verdict '{verdict}'. 허용 값: {', '.join(sorted(VALID_VERDICTS))}",
            file=sys.stderr,
        )
        sys.exit(2)

    data = _load_active_guards()

    # guards 리스트는 그대로 보존, sdc_brief_* 키만 업데이트
    now = datetime.now().isoformat()
    data["sdc_brief_submitted"] = True
    data["sdc_brief_verdict"] = verdict
    data["sdc_brief_submitted_at"] = now
    data["sdc_brief_task"] = task

    _save_active_guards(data)

    # audit log
    brief_snippet = brief[:500] if brief else ""
    log_entry = {
        "timestamp": now,
        "verdict": verdict,
        "task": task,
        "rationale": rationale,
        "brief_snippet": brief_snippet,
        "reset": False,
    }
    _append_log(log_entry)

    if as_json:
        result = {
            "status": "ok",
            "action": "submit",
            "verdict": verdict,
            "task": task,
            "submitted_at": now,
            "sdc_brief_submitted": True,
        }
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[sdc_commit] brief 제출 완료: verdict={verdict}, task={task!r}, at={now}")


def cmd_reset(as_json: bool) -> None:
    """sdc_brief_submitted=false 로 복원. verdict/at/task 키 제거 (stale 방지)."""
    data = _load_active_guards()

    # sdc_brief_submitted 만 false 로, 나머지 sdc_brief_* 키 제거
    data["sdc_brief_submitted"] = False
    for key in ("sdc_brief_verdict", "sdc_brief_submitted_at", "sdc_brief_task"):
        data.pop(key, None)

    _save_active_guards(data)

    now = datetime.now().isoformat()
    log_entry = {
        "timestamp": now,
        "verdict": "",
        "task": "",
        "rationale": "",
        "brief_snippet": "",
        "reset": True,
    }
    _append_log(log_entry)

    if as_json:
        result = {
            "status": "ok",
            "action": "reset",
            "sdc_brief_submitted": False,
            "reset_at": now,
        }
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[sdc_commit] reset 완료: sdc_brief_submitted=false, at={now}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdc_commit",
        description="SDC Auto-Dispatch Gate brief 제출 CLI (TCL #120)",
    )
    parser.add_argument(
        "--verdict",
        choices=sorted(VALID_VERDICTS),
        metavar="{" + ",".join(sorted(VALID_VERDICTS)) + "}",
        help="3-question gate 판정 결과 (필수, --reset 과 함께 사용 불가)",
    )
    parser.add_argument(
        "--task",
        default="",
        help="task 식별자 (선택)",
    )
    parser.add_argument(
        "--rationale",
        default="",
        help="KEEP 시 근거 1줄 또는 보조 설명 (선택)",
    )
    parser.add_argument(
        "--brief",
        default="",
        help="brief 요약 — audit log 에 500자까지 저장 (선택)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="sdc_brief_submitted=false 로 복원 (세션 시작 시 또는 테스트용)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="결과를 JSON 으로 stdout 출력",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.reset:
        # --reset 과 --verdict 동시 사용 금지
        if args.verdict:
            print("ERROR: --reset 과 --verdict 를 동시에 사용할 수 없습니다.", file=sys.stderr)
            sys.exit(2)
        cmd_reset(as_json=args.as_json)
        return

    # --verdict 필수 (--reset 없는 경우)
    if not args.verdict:
        print(
            "ERROR: --verdict {" + ",".join(sorted(VALID_VERDICTS)) + "} 는 필수입니다. "
            "(또는 --reset 사용)",
            file=sys.stderr,
        )
        sys.exit(2)

    cmd_submit(
        verdict=args.verdict,
        task=args.task,
        rationale=args.rationale,
        brief=args.brief,
        as_json=args.as_json,
    )


if __name__ == "__main__":
    main()
