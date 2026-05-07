"""
TEMS Retrospective Hook — Stop 이벤트 회고 (Phase 1 T4.1)
==========================================================
Claude Code의 Stop hook으로 등록되어, 에이전트 응답이 종료될 때마다 발동된다.
매 응답마다 발동되면 노이즈가 심하므로 RATE_LIMIT_SEC 간격으로만 실제 분석.

자동등록 모드(TCL '자동등록 활성화' 등록 시):
  - 임계값 이상 반복 패턴은 tems_commit.py로 자동 등록
수동 모드(기본):
  - 후보를 stdout으로 출력 (운영자가 검토 후 수동 등록)

S60 compliance reform — citation parser:
  - 매 Stop 마다 (rate-limit 무관) transcript 의 마지막 assistant turn 에서
    `TGL #N` / `TCL #N` 인용 패턴을 추출하여 active_compliance_count 누적.
  - LLM 이 실제로 룰을 인지·인용한 신호 (passive compliance_count 와 분리).
  - 동일 turn 중복 처리 방지: .retrospective_last_processed_uuid 캐시.

stdout: <tems-retrospective> 블록 (출력 시 에이전트 컨텍스트에 주입됨)
"""

import sys
import json
import re
import sqlite3
import traceback
from pathlib import Path
from datetime import datetime

MEMORY_DIR = Path(__file__).resolve().parent  # v0.4: cwd 비의존
DB_PATH = MEMORY_DIR / "error_logs.db"
DIAG_PATH = MEMORY_DIR / "tems_diagnostics.jsonl"
sys.path.insert(0, str(MEMORY_DIR.parent))

# Rate limit — Stop hook은 매 응답 끝마다 발동되므로 N초 간격으로만 실행
RATE_LIMIT_SEC = 600  # 10분
RATE_FILE = MEMORY_DIR / ".retrospective_last_run"
PROCESSED_UUID_FILE = MEMORY_DIR / ".retrospective_last_processed_uuid"
MAX_REPORTED = 3       # 한 번에 표시할 최대 후보 수

# 인용 패턴: "TGL #N", "TGL#N", "TGL N", "TCL #N", "TGL: #N" 등.
# preflight 가 강제하는 "주입된 TGL: #X" / "TGL #X 에 따라" 패턴을 모두 포착.
# 콜론·공백 mix 도 허용. \b 는 한국어 인접 문맥 (예: "주입된 TGL") 에서도 작동.
CITATION_PATTERN = re.compile(r"\b(TGL|TCL)[\s:]*#?\s*(\d{1,5})\b", re.IGNORECASE)


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
    try:
        with open(DIAG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "event": event_type,
                "exc_type": type(exc).__name__,
                "exc_msg": str(exc)[:300],
                "traceback": traceback.format_exc()[-800:],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _last_assistant_turn(transcript_path: Path) -> dict | None:
    """transcript jsonl 에서 마지막 assistant 메시지 entry 반환.

    Claude Code transcript 는 한 줄 = 한 entry. assistant entry 는
    {"type": "assistant", "message": {...}, "uuid": "...", ...} 형태.
    """
    if not transcript_path or not transcript_path.exists():
        return None
    last_assistant = None
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "assistant" or entry.get("role") == "assistant":
                    last_assistant = entry
    except OSError as e:
        _log_diagnostic("retrospective_transcript_read_failure", e)
        return None
    return last_assistant


def _extract_text(assistant_entry: dict) -> str:
    """assistant entry 의 응답 본문을 평문으로 추출."""
    if not assistant_entry:
        return ""
    msg = assistant_entry.get("message") or assistant_entry
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Claude API 멀티모달: [{"type":"text","text":"..."}, ...]
        chunks = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                chunks.append(c.get("text", ""))
            elif isinstance(c, str):
                chunks.append(c)
        return "\n".join(chunks)
    # fallback
    text = msg.get("text") or assistant_entry.get("text") or ""
    return str(text)


def parse_citations(text: str) -> set[int]:
    """본문에서 TGL/TCL #N 인용을 추출. preflight 자체가 출력하는 인용 예시
    (예: '예: "TGL #54 에 따라"') 까지 포함되지만, 그 노이즈는 preflight injection
    이 LLM 응답에 그대로 포함된 경우만 발생하며 — 그 자체가 LLM 이 룰을
    수용한 신호로 해석 가능하므로 별도 스킵 로직은 두지 않는다.
    """
    cited = set()
    if not text:
        return cited
    for m in CITATION_PATTERN.finditer(text):
        try:
            cited.add(int(m.group(2)))
        except ValueError:
            continue
    return cited


def update_active_compliance(rule_ids: set[int]) -> int:
    """rule_health.active_compliance_count + last_active_compliance_at 갱신."""
    if not rule_ids:
        return 0
    if not DB_PATH.exists():
        return 0
    now_iso = datetime.now().isoformat()
    updated = 0
    try:
        conn = sqlite3.connect(str(DB_PATH))
        for rid in rule_ids:
            conn.execute("""
                INSERT INTO rule_health
                    (rule_id, active_compliance_count, last_active_compliance_at, ths_score, status)
                VALUES (?, 1, ?, 0.5, 'warm')
                ON CONFLICT(rule_id) DO UPDATE SET
                    active_compliance_count = COALESCE(active_compliance_count, 0) + 1,
                    last_active_compliance_at = ?
            """, (rid, now_iso, now_iso))
            updated += 1
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as e:
        # 구 스키마 (active_compliance_count 컬럼 부재) — 마이그레이션 안 된 경우
        _log_diagnostic("retrospective_active_compliance_schema_missing", e)
        return 0
    except Exception as e:
        _log_diagnostic("retrospective_active_compliance_update_failure", e)
        return 0
    return updated


def process_citations(transcript_path: Path) -> dict:
    """Stop hook 매 발동마다 호출. rate-limit 과 무관하게 인용은 항상 측정."""
    last = _last_assistant_turn(transcript_path)
    if not last:
        return {"processed": False, "reason": "no_assistant_turn"}
    uuid = last.get("uuid") or last.get("id") or ""
    # 동일 turn 중복 카운트 방지
    if uuid and PROCESSED_UUID_FILE.exists():
        try:
            if PROCESSED_UUID_FILE.read_text(encoding="utf-8").strip() == uuid:
                return {"processed": False, "reason": "already_processed", "uuid": uuid}
        except Exception:
            pass
    text = _extract_text(last)
    cited = parse_citations(text)
    n = update_active_compliance(cited)
    if uuid:
        try:
            PROCESSED_UUID_FILE.write_text(uuid, encoding="utf-8")
        except Exception:
            pass
    return {"processed": True, "uuid": uuid, "cited": sorted(cited), "updated": n}


def main():
    # citation 측정은 rate-limit 무관 — 매 Stop 마다 실행
    raw_stdin = ""
    try:
        if not sys.stdin.isatty():
            raw_stdin = sys.stdin.read()
    except Exception:
        pass

    transcript_path = None
    if raw_stdin.strip():
        try:
            payload = json.loads(raw_stdin)
            tp = payload.get("transcript_path")
            if tp:
                transcript_path = Path(tp)
        except Exception as e:
            _log_diagnostic("retrospective_stdin_parse_failure", e)

    if transcript_path:
        try:
            process_citations(transcript_path)
        except Exception as e:
            _log_diagnostic("retrospective_citation_processing_failure", e)

    # Pattern detection 은 기존대로 rate-limit 적용
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
