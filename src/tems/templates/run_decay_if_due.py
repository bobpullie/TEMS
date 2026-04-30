"""SessionStart wrapper — last_run 24h 가드로 decay.py 호출.

cron 대체 (v0.4 — PC 가 꺼져있어도 다음 세션 시작 시 따라잡음).
fail-soft 설계 — decay 실패가 세션 부트를 막지 않음.

동작:
  1. memory/.decay_last_run 의 mtime 읽기
  2. (now - mtime) < 24h 이면 즉시 종료 (no-op)
  3. 그 외엔 decay.py --json 실행, 결과를 tems_diagnostics.jsonl 에 append
  4. 마커 파일 touch
  5. 어떤 실패도 stdout/stderr 출력 없이 fail-soft (hook 컨텍스트 오염 0)

종료 코드: 항상 0 (hook 차단 방지)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = ROOT / "memory" / ".decay_last_run"
DIAG = ROOT / "memory" / "tems_diagnostics.jsonl"
DECAY = ROOT / "memory" / "decay.py"

INTERVAL_SECONDS = 24 * 3600


def _log(event: str, payload: dict) -> None:
    try:
        with DIAG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "event": event,
                **payload,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        if MARKER.exists():
            age = time.time() - MARKER.stat().st_mtime
            if age < INTERVAL_SECONDS:
                return 0  # 24h 안 됨 → skip

        if not DECAY.exists():
            _log("decay_skip_missing", {"path": str(DECAY)})
            return 0

        proc = subprocess.run(
            [sys.executable, str(DECAY), "--json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

        try:
            result = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except Exception:
            result = {"raw_stdout": proc.stdout[:500]}

        _log("decay_run", {
            "returncode": proc.returncode,
            "transitions": result.get("transitions"),
            "to_cold": result.get("to_cold"),
            "to_archive": result.get("to_archive"),
            "stderr_tail": (proc.stderr or "")[-300:],
        })

        MARKER.touch()
    except Exception as e:
        _log("decay_wrapper_error", {"err": type(e).__name__, "msg": str(e)[:200]})

    return 0


if __name__ == "__main__":
    sys.exit(main())
