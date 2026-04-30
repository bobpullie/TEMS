"""
TEMS Recent Diagnostics Reporter (v0.4 — SessionStart α layer)
================================================================
SessionStart 시점에 `tems_diagnostics.jsonl` 의 최근 N시간 내 `*_failure` 이벤트를
stdout 으로 표시. 에이전트가 자기보고 (핸드오버/recap) 작성 전 production failure
누락하는 메타-결함을 차단하기 위한 가시화 레이어.

Read-only — DB 미접속, jsonl 만 read.

Usage:
  python memory/audit_diagnostics_recent.py             # 24h, human stdout
  python memory/audit_diagnostics_recent.py --hours 48  # window 조정
  python memory/audit_diagnostics_recent.py --json      # JSON 출력
  python memory/audit_diagnostics_recent.py --silent    # failure 0건이면 출력 X

stdin: 없음 (SessionStart hook 은 stdin 미사용)
stdout: 사람용 텍스트 또는 JSON
exit: 0 (항상 — hook 차단 방지)
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# .resolve() canonical pattern (v0.4 — cwd 비의존)
MEMORY_DIR = Path(__file__).resolve().parent
DIAG_PATH = MEMORY_DIR / "tems_diagnostics.jsonl"
PENDING_DIR = MEMORY_DIR / "pending_self_cognition"

FAILURE_SUFFIX = "_failure"


def _log_diagnostic(event: str, exc: Exception) -> None:
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


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def collect_failures(hours: int) -> list[dict]:
    """jsonl 마지막 줄부터 read 하여 hours 내 *_failure 이벤트만 추출.

    파일이 없거나 비어있으면 빈 리스트. 큰 파일에 대비해 끝에서부터 read.
    """
    if not DIAG_PATH.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    out: list[dict] = []
    try:
        with open(DIAG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        _log_diagnostic("audit_diagnostics_recent_read_failure", e)
        return []

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_name = evt.get("event") or ""
        if not event_name.endswith(FAILURE_SUFFIX):
            continue
        ts = _parse_ts(evt.get("timestamp", ""))
        if ts is None or ts < cutoff:
            # 시간 역순이라 cutoff 미만이면 더 과거 — 중단 안 함
            # (timestamp 누락 라인이 사이에 끼었을 수 있음)
            if ts is not None:
                # cutoff 명확히 넘어서면 break
                break
            continue
        out.append(evt)

    out.reverse()  # 시간 정순
    return out


def collect_stale_pending(hours: int = 24) -> list[dict]:
    """pending_self_cognition/*.json 중 24h+ 미해소 draft 가시화.

    self_cognition_gate (γ layer) 가 사용하는 pending 디렉토리. 본 디렉토리가
    부재하면 빈 리스트 (gate 미설치 환경에서 안전).
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    stale: list[dict] = []
    try:
        drafts = sorted(PENDING_DIR.glob("*.json"))
    except OSError as e:
        _log_diagnostic("audit_self_cognition_pending_list_failure", e)
        return []
    for draft_path in drafts:
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
        except OSError as e:
            _log_diagnostic("audit_self_cognition_pending_read_failure", e)
            continue
        except json.JSONDecodeError as e:
            _log_diagnostic("audit_self_cognition_pending_parse_failure", e)
            continue
        created = _parse_ts(str(draft.get("created_at", "")))
        if created is None:
            try:
                created = datetime.fromtimestamp(draft_path.stat().st_mtime)
            except OSError as e:
                _log_diagnostic("audit_self_cognition_pending_stat_failure", e)
                continue
        compare_created = created.replace(tzinfo=None) if created.tzinfo else created
        if compare_created <= cutoff:
            stale.append({
                "draft_id": draft.get("draft_id") or draft_path.stem,
                "created_at": draft.get("created_at") or created.isoformat(),
                "signal_type": draft.get("signal_type", "?"),
                "priority": draft.get("priority", "?"),
                "path": str(draft_path),
            })
    stale.sort(key=lambda item: item.get("created_at", ""))
    return stale


def format_stale_pending(stale: list[dict]) -> str:
    if not stale:
        return ""
    oldest = stale[0].get("created_at", "?")
    lines = [f"<self-cognition-stale count=\"{len(stale)}\" oldest=\"{oldest}\">"]
    for draft in stale[:5]:
        lines.append(
            f"  [{draft.get('created_at', '?')}] {draft.get('draft_id', '?')} "
            f"signal={draft.get('signal_type', '?')} priority={draft.get('priority', '?')}"
        )
    if len(stale) > 5:
        lines.append(f"  ... +{len(stale) - 5}건 더")
    lines.append("</self-cognition-stale>")
    return "\n".join(lines) + "\n"


def format_human(failures: list[dict], hours: int, stale_pending: list[dict] | None = None) -> str:
    stale_text = format_stale_pending(stale_pending or [])
    if not failures:
        return f"=== TEMS Recent Failures ({hours}h) ===\n(no failure events — clear)\n" + stale_text
    lines = [f"=== TEMS Recent Failures ({hours}h) ==="]
    for evt in failures:
        ts = evt.get("timestamp", "?")
        event = evt.get("event", "?")
        exc_type = evt.get("exc_type", "")
        exc_msg = (evt.get("exc_msg") or "").replace("\n", " ")[:200]
        head = f"  [{ts}] {event}"
        if exc_type:
            head += f"  ({exc_type})"
        lines.append(head)
        if exc_msg:
            lines.append(f"    msg: {exc_msg}")
    lines.append(
        f"  → 자기보고 (핸드오버/recap) 작성 시 위 failure 의 후속 처치/원인 누락 금지. "
        f"jsonl 전수: {DIAG_PATH}"
    )
    return "\n".join(lines) + "\n" + stale_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--silent", action="store_true",
                        help="failure 0건이면 stdout 출력 X (SessionStart 노이즈 감소)")
    args = parser.parse_args()

    failures = collect_failures(args.hours)
    stale_pending = collect_stale_pending(24)

    if args.silent and not failures and not stale_pending:
        return 0

    if args.as_json:
        out = {
            "hours": args.hours,
            "count": len(failures),
            "failures": failures,
            "self_cognition_stale": stale_pending,
            "diag_path": str(DIAG_PATH),
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_human(failures, args.hours, stale_pending))
    return 0


if __name__ == "__main__":
    sys.exit(main())
