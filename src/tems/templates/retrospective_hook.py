"""
TEMS Retrospective Hook — Stop 이벤트 회고 (Phase 1 T4.1)
==========================================================
Claude Code의 Stop hook으로 등록되어, 에이전트 응답이 종료될 때마다 발동된다.
매 응답마다 발동되면 노이즈가 심하므로 RATE_LIMIT_SEC 간격으로만 실제 분석.

자동등록 모드(TCL '자동등록 활성화' 등록 시):
  - 임계값 이상 반복 패턴은 tems_commit.py로 자동 등록
수동 모드(기본):
  - 후보를 stdout으로 출력 (운영자가 검토 후 수동 등록)

stdout: <tems-retrospective> 블록 (출력 시 에이전트 컨텍스트에 주입됨)
"""

import sys
import json
from pathlib import Path
from datetime import datetime

MEMORY_DIR = Path(__file__).parent
sys.path.insert(0, str(MEMORY_DIR.parent))

# Rate limit — Stop hook은 매 응답 끝마다 발동되므로 N초 간격으로만 실행
RATE_LIMIT_SEC = 600  # 10분
RATE_FILE = MEMORY_DIR / ".retrospective_last_run"
MAX_REPORTED = 3       # 한 번에 표시할 최대 후보 수


def should_run() -> bool:
    if not RATE_FILE.exists():
        return True
    try:
        last = float(RATE_FILE.read_text(encoding='utf-8').strip())
        now = datetime.now().timestamp()
        return (now - last) >= RATE_LIMIT_SEC
    except Exception:
        return True


def mark_run() -> None:
    try:
        RATE_FILE.write_text(str(datetime.now().timestamp()), encoding='utf-8')
    except Exception:
        pass


def _log_diagnostic(event_type: str, exc: Exception) -> None:
    """T1.1 원칙 — silent fail 금지. 실패도 진단 채널에 적재."""
    import traceback
    try:
        with open(MEMORY_DIR / "tems_diagnostics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "event": event_type,
                "exc_type": type(exc).__name__,
                "exc_msg": str(exc)[:300],
                "traceback": traceback.format_exc()[-800:],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    if not should_run():
        sys.exit(0)
    mark_run()

    try:
        from memory.pattern_detector import (
            detect_patterns, is_auto_register_enabled,
            generate_tgl_text, auto_register, is_already_registered_pattern,
            AUTO_REGISTER_THRESHOLD,
        )

        candidates = detect_patterns()
        if not candidates:
            sys.exit(0)

        # 이미 등록된 패턴 제외
        new_candidates = [c for c in candidates if not is_already_registered_pattern(c['pattern_key'])]
        if not new_candidates:
            sys.exit(0)

        auto_mode = is_auto_register_enabled()

        lines = [f"<tems-retrospective auto_register=\"{'on' if auto_mode else 'off'}\" candidates=\"{len(new_candidates)}\">"]
        for c in new_candidates[:MAX_REPORTED]:
            text = generate_tgl_text(c)
            lines.append(f"  [{c['severity']}] {c['signature']} ×{c['count']}회 반복")
            lines.append(f"    예시: {c['sample_detail'][:120]}")
            lines.append(f"    명령 패턴: {c['top_cmd_pattern']} (diversity={c['cmd_diversity']})")

            if auto_mode and c['count'] >= AUTO_REGISTER_THRESHOLD:
                r = auto_register(text)
                if r.get('ok'):
                    lines.append(f"    → 자동등록 완료: TGL #{r['rule_id']}")
                else:
                    lines.append(f"    → 자동등록 실패: {r.get('error', '?')[:80]} — 수동 등록 권장")
            else:
                threshold_note = "" if auto_mode else " (자동등록 모드 비활성)"
                count_note = "" if c['count'] >= AUTO_REGISTER_THRESHOLD else f" (count<{AUTO_REGISTER_THRESHOLD})"
                lines.append(f"    → 운영자 검토 후 등록{threshold_note}{count_note}:")
                lines.append(f'      python memory/tems_commit.py --type TGL --rule "..." --triggers "{text["triggers"][:80]}" --tags "{text["tags"]}"')
        lines.append("</tems-retrospective>")
        print("\n".join(lines))

    except Exception as e:
        # T1.1 원칙: silent fail 금지
        _log_diagnostic("retrospective_failure", e)
        print(f"<retrospective-degraded reason=\"{type(e).__name__}: {str(e)[:120]}\"/>")

    sys.exit(0)


if __name__ == "__main__":
    main()
