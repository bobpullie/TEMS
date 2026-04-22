"""
TEMS Pattern Detector — 자기관찰 jsonl 채널에서 반복 실수 패턴 추출 (Phase 1 T1.3)
=====================================================================================
tool_failures.jsonl + tems_diagnostics.jsonl을 스캔하여 동일 시그니처 N회 이상
반복된 실패를 후보 TGL로 일반화한다. 자가진화 메모리 시스템(Topological *Evolving*
Memory System)의 'Evolving' 부분을 구현하는 핵심 모듈.

CLI:
  python memory/pattern_detector.py [--min-count N] [--json]
"""

import json
import re
import sys
import argparse
import sqlite3
import subprocess
from pathlib import Path
from collections import defaultdict
from datetime import datetime

MEMORY_DIR = Path(__file__).parent
TOOL_FAILURES = MEMORY_DIR / "tool_failures.jsonl"
TEMS_DIAGNOSTICS = MEMORY_DIR / "tems_diagnostics.jsonl"
DB_PATH = MEMORY_DIR / "error_logs.db"

REPETITION_THRESHOLD = 3   # 동일 패턴 N회 이상이면 후보
AUTO_REGISTER_THRESHOLD = 5  # 자동등록은 더 보수적 (N회 이상 + 자동모드 활성)
RECENT_LIMIT = 500          # jsonl 최근 N줄만 분석 (전수 스캔 비용 절감)


# ═══════════════════════════════════════════════════════════
# I/O
# ═══════════════════════════════════════════════════════════

def load_jsonl(path: Path, limit_recent: int = RECENT_LIMIT) -> list[dict]:
    """jsonl 파일에서 최근 N줄 로드. 파싱 실패 줄은 건너뜀."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding='utf-8').strip().split('\n')
    except Exception:
        return []
    out = []
    for line in lines[-limit_recent:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ═══════════════════════════════════════════════════════════
# 정규화 (그룹화 키 생성)
# ═══════════════════════════════════════════════════════════

def normalize_signature_detail(sig: str, detail: str) -> str:
    """시그니처 + detail을 정규화하여 그룹화 키 생성.
    가변 부분(파일 경로, 숫자, 따옴표 안 문자열) 제거.
    """
    detail = detail or ""
    detail = re.sub(r"['\"][^'\"]*['\"]", "X", detail)
    detail = re.sub(r"\d+", "N", detail)
    detail = re.sub(r"[A-Za-z]:[/\\][\w./\\-]+", "PATH", detail)  # Windows 경로
    detail = re.sub(r"/[\w./-]+", "PATH", detail)                   # Unix 경로
    return f"{sig}:{detail.strip()[:80]}"


def normalize_cmd(cmd: str) -> str:
    """명령어 정규화 — 변수 부분 제거"""
    cmd = cmd or ""
    cmd = re.sub(r"['\"][^'\"]*['\"]", "X", cmd)
    cmd = re.sub(r"\d+", "N", cmd)
    return cmd.strip()[:80]


# ═══════════════════════════════════════════════════════════
# 패턴 감지
# ═══════════════════════════════════════════════════════════

def detect_patterns(min_count: int = REPETITION_THRESHOLD) -> list[dict]:
    """jsonl을 스캔하여 반복 패턴 후보 추출. count 내림차순."""
    failures = load_jsonl(TOOL_FAILURES)
    diagnostics = load_jsonl(TEMS_DIAGNOSTICS)

    groups: dict[str, list[dict]] = defaultdict(list)

    # tool_failures: matches 배열 펼치기
    for entry in failures:
        ts = entry.get('timestamp', '')
        cmd = entry.get('cmd_summary', '')
        for match in entry.get('matches', []):
            sig = match.get('signature', '')
            detail = match.get('detail', '')
            key = normalize_signature_detail(sig, detail)
            groups[key].append({
                'source': 'tool_failure',
                'timestamp': ts,
                'cmd': cmd,
                'cmd_norm': normalize_cmd(cmd),
                'severity': match.get('severity', 'medium'),
                'sig': sig,
                'detail': detail,
            })

    # diagnostics: preflight 실패 자체
    for entry in diagnostics:
        ts = entry.get('timestamp', '')
        exc_type = entry.get('exc_type', '')
        exc_msg = entry.get('exc_msg', '')
        key = normalize_signature_detail(f"preflight_{exc_type}", exc_msg)
        groups[key].append({
            'source': 'preflight_diagnostic',
            'timestamp': ts,
            'cmd': '<preflight_hook>',
            'cmd_norm': '<preflight_hook>',
            'severity': 'critical',
            'sig': f"preflight_{exc_type}",
            'detail': exc_msg,
        })

    candidates = []
    for key, items in groups.items():
        if len(items) < min_count:
            continue
        cmd_counter = defaultdict(int)
        for it in items:
            cmd_counter[it['cmd_norm']] += 1
        top_cmd = max(cmd_counter, key=cmd_counter.get)

        first = items[0]
        last = items[-1]
        candidates.append({
            'pattern_key': key,
            'count': len(items),
            'severity': first['severity'],
            'signature': first['sig'],
            'sample_detail': first['detail'][:150],
            'top_cmd_pattern': top_cmd[:100],
            'cmd_diversity': len(cmd_counter),
            'first_seen': first['timestamp'],
            'last_seen': last['timestamp'],
            'source': first['source'],
        })

    candidates.sort(key=lambda c: (c['count'], c['severity'] == 'critical'), reverse=True)
    return candidates


# ═══════════════════════════════════════════════════════════
# 일반화 → TGL 후보 텍스트 생성
# ═══════════════════════════════════════════════════════════

def generate_tgl_text(candidate: dict) -> dict:
    """후보 패턴을 TGL 등록 가능한 형태로 일반화.
    위상화는 부분적 — 위상군이 검토 시 추상화 보강 필요.
    """
    sig = candidate['signature']
    detail = candidate['sample_detail']
    cmd = candidate['top_cmd_pattern']
    count = candidate['count']
    diversity = candidate['cmd_diversity']
    sev = candidate['severity']

    diversity_note = (
        "동일 명령에서만 발생 (정확한 재현)" if diversity == 1
        else f"{diversity}개 명령 패턴에서 발생 (광범위)"
    )

    rule = (
        f"[자동감지 패턴 / {count}회 반복 / severity={sev}] "
        f"{sig}: {detail[:100]}. "
        f"발생 컨텍스트: {cmd[:80]} ({diversity_note}). "
        f"근본 원인 조사 후 가드 행동/대안 명시 필수. "
        f"(자동 생성 — 등록 전 위상군이 추상화·일반화 보강할 것)"
    )

    # trigger: signature + 핵심 명사 추출
    sig_words = sig.replace('_', ' ')
    detail_first = (detail.split()[0] if detail else '').strip(":\"',()[]{}")
    triggers = f"{sig_words} {detail_first} 자동감지 반복 패턴".strip()

    return {
        'category': 'TGL',
        'rule': rule[:500],
        'triggers': triggers[:200],
        'tags': f"auto-detected,pattern,{sig}",
        'source_count': count,
    }


# ═══════════════════════════════════════════════════════════
# 자동등록 모드 토글 (TCL 기반)
# ═══════════════════════════════════════════════════════════

def is_auto_register_enabled() -> bool:
    """DB에 'TEMS 자동등록 활성화' TCL이 활성(non-archive) 상태로 존재하면 True.

    종일군이 명시적으로 모드 전환 TCL을 등록하면 활성화. 등록 해제 또는
    archive 처리 시 비활성화.
    """
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT m.id FROM memory_logs m
            LEFT JOIN rule_health h ON h.rule_id = m.id
            WHERE m.category = 'TCL'
              AND (
                  m.keyword_trigger LIKE '%TEMS 자동등록 활성화%'
                  OR m.correction_rule LIKE '%TEMS 자동등록 활성화%'
                  OR m.correction_rule LIKE '%auto-register-enabled%'
              )
              AND (h.status IS NULL OR h.status != 'archive')
            LIMIT 1
        """).fetchall()
        conn.close()
        return len(rows) > 0
    except Exception:
        return False


def is_already_registered_pattern(pattern_key: str) -> bool:
    """동일 pattern_key로 이미 자동등록된 규칙이 있는지 (auto-detected 태그 + signature 매칭)."""
    if not DB_PATH.exists():
        return False
    sig = pattern_key.split(':', 1)[0] if ':' in pattern_key else pattern_key
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id FROM memory_logs
            WHERE category = 'TGL'
              AND context_tags LIKE ?
              AND context_tags LIKE '%auto-detected%'
            LIMIT 1
        """, (f"%{sig}%",)).fetchall()
        conn.close()
        return len(rows) > 0
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
# 자동 등록 (tems_commit.py 호출)
# ═══════════════════════════════════════════════════════════

def auto_register(candidate_text: dict) -> dict:
    cmd = [
        sys.executable, str(MEMORY_DIR / 'tems_commit.py'),
        '--type', candidate_text['category'],
        '--rule', candidate_text['rule'],
        '--triggers', candidate_text['triggers'],
        '--tags', candidate_text['tags'],
        '--source', 'pattern-detector-auto',
        '--json',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        out = r.stdout.decode('utf-8', errors='replace').strip()
        return json.loads(out) if out else {'ok': False, 'error': 'empty stdout'}
    except Exception as e:
        return {'ok': False, 'error': f"{type(e).__name__}: {str(e)[:120]}"}


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="TEMS 패턴 감지기")
    parser.add_argument('--min-count', type=int, default=REPETITION_THRESHOLD)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--no-auto', action='store_true', help="자동등록 모드 무시 (수동 후보만 출력)")
    args = parser.parse_args()

    candidates = detect_patterns(min_count=args.min_count)
    if not candidates:
        if args.json:
            print(json.dumps({'candidates': [], 'auto_mode': False}, ensure_ascii=False))
        return

    auto_mode = (not args.no_auto) and is_auto_register_enabled()

    if args.json:
        result = {'candidates': [], 'auto_mode': auto_mode}
        for c in candidates:
            text = generate_tgl_text(c)
            result['candidates'].append({**c, 'tgl_text': text})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"=== TEMS Pattern Detector (auto_mode={'ON' if auto_mode else 'OFF'}) ===")
    print(f"발견된 후보: {len(candidates)}개 (min_count={args.min_count})\n")
    for i, c in enumerate(candidates, 1):
        text = generate_tgl_text(c)
        print(f"[{i}] {c['signature']} ×{c['count']} (severity={c['severity']})")
        print(f"    sample: {c['sample_detail'][:100]}")
        print(f"    top_cmd: {c['top_cmd_pattern']}")
        print(f"    diversity: {c['cmd_diversity']}, first={c['first_seen'][:19]}, last={c['last_seen'][:19]}")

        if is_already_registered_pattern(c['pattern_key']):
            print(f"    → SKIP: 이미 auto-detected 태그로 등록된 패턴")
        elif auto_mode and c['count'] >= AUTO_REGISTER_THRESHOLD:
            r = auto_register(text)
            if r.get('ok'):
                print(f"    → 자동등록 완료: TGL #{r['rule_id']}")
            else:
                print(f"    → 자동등록 실패: {r.get('error', '?')}")
        else:
            mode_note = "수동 모드" if not auto_mode else f"count<{AUTO_REGISTER_THRESHOLD} (자동등록 임계 미달)"
            print(f"    → 등록 권장 ({mode_note}):")
            print(f'       python memory/tems_commit.py --type TGL \\')
            print(f'         --rule "{text["rule"][:80]}..." \\')
            print(f'         --triggers "{text["triggers"]}" --tags "{text["tags"]}"')
        print()


if __name__ == "__main__":
    main()
