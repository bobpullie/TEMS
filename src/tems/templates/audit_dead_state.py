"""TEMS Schema/Method Dead-State Audit
=====================================
DB 컬럼과 핵심 클래스 메서드의 producer/consumer/caller 를 grep 으로
역추적해 dead-state 후보를 검출한다. 회귀 방지 자동화 도구.

검출 대상:
  - rule_health 테이블 (TEMS health-tracking layer 의 핵심)
  - tems_engine.py 의 HealthScorer / MetaRuleEngine 클래스 메서드

판정:
  - column: write count == 0 OR read count == 0  → 'dead'
  - method: caller count == 0  → 'dead'
  - column write/read 양쪽 모두 0 또는 method caller 0 → 'fully_dead'

v0.4 보강 (자기진단 결함 정정):
  - 클래스명 정정 — 실재하는 HealthScorer + MetaRuleEngine 만 audit
  - column write grep 에 f-string 변수 컬럼명 패턴 추가 (compliance_tracker.update_counts
    처럼 `INSERT INTO {table} (..., {col}, ...)` 형태로 컬럼명을 동적 inject 하는
    production code 인식)
  - scorer-style caller 인식 (self.scorer.compute_ths / scorer.compute_ths)

실행 (에이전트 프로젝트 루트에서):
  python memory/audit_dead_state.py            # 사람용 리포트
  python memory/audit_dead_state.py --json     # JSON
  python memory/audit_dead_state.py --silent   # dead 0건이면 침묵 (hook 용)

종료 코드:
  0 = dead 0건  /  1 = dead 발견  /  2 = audit 자체 실패
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "memory" / "error_logs.db"

AUDIT_TABLES = ["rule_health"]
AUDIT_CLASSES = {
    "memory/tems_engine.py": ["HealthScorer", "MetaRuleEngine"],
}

# v0.4: f-string 변수 컬럼명을 사용하는 production code 패턴.
# compliance_tracker.update_counts 처럼 `col = f"{field}_count"` 후 SQL inject.
# 이 패턴을 감지하기 위해 grep 시 컬럼명을 직접 찾는 게 아니라,
# (1) 변수 inject 대상 함수 시그니처를 알고 있는 화이트리스트로 대응
# (2) 또는 grep 결과에 dynamic-write 후보를 추가
DYNAMIC_WRITE_PATTERNS = [
    # compliance_tracker.update_counts: rule_health 의 compliance_count/violation_count 동적 처리
    (r'compliance_count|violation_count',
     'memory/compliance_tracker.py',
     ['compliance_count', 'violation_count']),
]

SEARCH_GLOB = ["memory/*.py", "viewer/*.py"]
EXCLUDE_DIRS = ("_backup_tier1", ".scratch_tems_work", "__pycache__")


def _grep(pattern: str, multiline: bool = False) -> list[str]:
    """ripgrep 기반 검색. 결과 라인 list (path:line:text) 반환.

    v0.4 보강: multiline=True 시 -U --multiline-dotall 로 SQL 다중 라인 SET 절 인식.
    """
    cmd = [
        "rg", "-n", "--no-heading",
        "-g", "*.py",
    ]
    if multiline:
        cmd += ["-U", "--multiline-dotall"]
    for d in EXCLUDE_DIRS:
        cmd += ["-g", f"!{d}/**"]
    cmd += [pattern, "memory", "viewer"]
    try:
        out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        if out.returncode not in (0, 1):
            return []
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    except FileNotFoundError:
        return _grep_fallback(pattern, multiline=multiline)
    except Exception:
        return []


def _grep_fallback(pattern: str, multiline: bool = False) -> list[str]:
    """ripgrep 없으면 순수 파이썬으로 fallback. multiline=True 시 . 가 \\n 매칭."""
    flags = re.DOTALL if multiline else 0
    pat = re.compile(pattern, flags)
    hits = []
    for glob in SEARCH_GLOB:
        for p in ROOT.glob(glob):
            if any(d in p.parts for d in EXCLUDE_DIRS):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
                if multiline:
                    # multiline 매칭: 매치 시 첫 줄 line number 보고
                    for m in pat.finditer(text):
                        line_no = text[:m.start()].count("\n") + 1
                        first_line = text[m.start():m.end()].splitlines()[0][:200]
                        hits.append(f"{p.relative_to(ROOT)}:{line_no}:{first_line}")
                else:
                    for i, line in enumerate(text.splitlines(), 1):
                        if pat.search(line):
                            hits.append(f"{p.relative_to(ROOT)}:{i}:{line}")
            except Exception:
                continue
    return hits


def _table_columns(table: str) -> list[str]:
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(str(DB_PATH))
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        con.close()


def _dynamic_write_files(col: str) -> list[str]:
    """v0.4 보강: f-string 변수로 컬럼명을 inject 하는 production code 의 file 목록.

    예: compliance_tracker.update_counts 가 `col = f"{field}_count"` + SQL inject.
    static grep 으로는 컬럼명 자체가 코드에 없어 잡히지 않으므로, 화이트리스트로 보강.
    """
    hits = []
    for _pat, file_path, cols in DYNAMIC_WRITE_PATTERNS:
        if col in cols:
            full = ROOT / file_path
            if full.exists():
                hits.append(file_path)
    return hits


def _wildcard_select_files(table: str) -> list[str]:
    """v0.4: `SELECT * FROM <table>` 형태 — 모든 컬럼이 잠재적으로 read 됨.

    verdict 자체는 바꾸지 않음 (명시적 read 가 없으면 wildcard 라도 컬럼이 실제로
    사용된다고 단언 불가). 단지 정보 표시용 — operator 가 dead 판정의 신뢰도를
    가늠하도록 wildcard SELECT 가 있는 파일 목록 제공.
    """
    hits = _grep(rf"SELECT\s+\*\s+FROM\s+{table}\b", multiline=True)
    return sorted({h.split(":")[0] for h in hits})


def audit_column(table: str, col: str) -> dict:
    """컬럼의 write/read 위치 검출.

    write = UPDATE ... SET col / INSERT ... col / SET col= 패턴
    read  = SELECT col / r["col"] / row["col"] / dict access 패턴
    """
    write_hits = []
    # 단일 라인 패턴
    write_hits += _grep(rf"SET\s+{col}\s*=")
    write_hits += _grep(rf",\s*{col}\s*=")  # multi-column SET 절: SET a=?, b=?
    write_hits += _grep(rf'\b{col}\s*=\s*excluded\.{col}')
    # multi-line SQL 패턴 (UPDATE..SET col1=?, col2=? OR INSERT INTO t (..,col,..))
    # rg -U 모드 — SQL 이 여러 줄에 걸쳐 있어도 인식
    write_hits += _grep(rf"UPDATE\s+{table}[\s\S]*?SET[\s\S]*?\b{col}\s*=", multiline=True)
    write_hits += _grep(rf"INSERT\s+INTO\s+{table}[\s\S]*?\b{col}\b[\s\S]*?VALUES", multiline=True)

    read_hits = []
    read_hits += _grep(rf"SELECT[^;]*\b{col}\b")
    read_hits += _grep(rf"SELECT[\s\S]*?\b{col}\b[\s\S]*?FROM", multiline=True)  # multi-line SELECT
    read_hits += _grep(rf'\["{col}"\]')
    read_hits += _grep(rf"\['{col}'\]")
    read_hits += _grep(rf"\.get\(['\"]?{col}['\"]?")
    read_hits += _grep(rf'\brow\s*\[["\']?{col}["\']?')

    write_files = sorted({h.split(":")[0] for h in write_hits})
    read_files = sorted({h.split(":")[0] for h in read_hits})

    # v0.4 보강: f-string dynamic write 파일 합산
    dyn_write = _dynamic_write_files(col)
    if dyn_write:
        write_files = sorted(set(write_files) | set(dyn_write))

    # v0.4: wildcard SELECT 정보 (verdict 변경 X, 정보 첨부)
    wildcard_files = _wildcard_select_files(table)

    if not write_files and not read_files:
        verdict = "fully_dead"
    elif not write_files:
        verdict = "no_producer"
    elif not read_files:
        verdict = "no_consumer"
    else:
        verdict = "alive"

    return {
        "kind": "column",
        "name": f"{table}.{col}",
        "verdict": verdict,
        "write_files": write_files,
        "read_files": read_files,
        "write_count": len(write_hits) + len(dyn_write),
        "read_count": len(read_hits),
        "dynamic_write": dyn_write,
        "wildcard_select_files": wildcard_files,
    }


def audit_method(file_path: str, cls: str, method: str) -> dict:
    """클래스 메서드의 호출 위치 검출.

    caller = .method( 패턴.
    v0.4: 같은 파일 내 caller (self.method, scorer.method) 도 진짜 호출 — 단지
    클래스 내부 응집성. external 0 + internal 0 인 경우만 진짜 dead 로 판정.
    """
    caller_hits = _grep(rf"\.{method}\s*\(")
    external = [h for h in caller_hits if file_path not in h]
    internal = [h for h in caller_hits if file_path in h]

    caller_files = sorted({h.split(":")[0] for h in caller_hits})
    verdict = "no_caller" if not (external or internal) else "alive"

    return {
        "kind": "method",
        "name": f"{cls}.{method}",
        "defined_in": file_path,
        "verdict": verdict,
        "caller_files": caller_files,
        "caller_count": len(caller_hits),
        "external_caller_count": len(external),
        "internal_caller_count": len(internal),
    }


def discover_methods(file_path: str, target_classes: list[str]) -> list[tuple[str, str]]:
    """파일에서 target_classes 의 메서드 시그니처 추출 (cls, method) 튜플."""
    p = ROOT / file_path
    if not p.exists():
        return []
    src = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    found = []
    current_cls = None
    for line in src:
        m_cls = re.match(r"^class\s+(\w+)", line)
        if m_cls:
            current_cls = m_cls.group(1) if m_cls.group(1) in target_classes else None
            continue
        if not current_cls:
            continue
        m_def = re.match(r"^    def\s+(\w+)\s*\(", line)
        if m_def:
            method = m_def.group(1)
            if method.startswith("_"):
                continue
            found.append((current_cls, method))
    return found


def run_audit() -> dict:
    results = []
    for table in AUDIT_TABLES:
        cols = _table_columns(table)
        for col in cols:
            results.append(audit_column(table, col))

    for file_path, classes in AUDIT_CLASSES.items():
        for cls, method in discover_methods(file_path, classes):
            results.append(audit_method(file_path, cls, method))

    summary = {
        "total": len(results),
        "alive": sum(1 for r in results if r["verdict"] == "alive"),
        "fully_dead": sum(1 for r in results if r["verdict"] == "fully_dead"),
        "no_producer": sum(1 for r in results if r["verdict"] == "no_producer"),
        "no_consumer": sum(1 for r in results if r["verdict"] == "no_consumer"),
        "no_caller": sum(1 for r in results if r["verdict"] == "no_caller"),
    }
    return {"summary": summary, "items": results}


def format_human(report: dict) -> str:
    lines = []
    s = report["summary"]
    lines.append(f"=== TEMS Dead-State Audit ===")
    lines.append(f"total={s['total']}  alive={s['alive']}  "
                 f"dead(fully)={s['fully_dead']}  no_producer={s['no_producer']}  "
                 f"no_consumer={s['no_consumer']}  no_caller={s['no_caller']}")
    lines.append("")

    dead_items = [r for r in report["items"] if r["verdict"] != "alive"]
    if not dead_items:
        lines.append("[OK] no dead-state items detected.")
        return "\n".join(lines)

    lines.append("[DEAD CANDIDATES]")
    for r in dead_items:
        if r["kind"] == "column":
            wildcard_n = len(r.get("wildcard_select_files") or [])
            wildcard_hint = f"  +wildcard SELECT in {wildcard_n}f" if wildcard_n else ""
            lines.append(f"  - {r['name']:40s}  {r['verdict']:14s}  w={r['write_count']:2d} r={r['read_count']:2d}{wildcard_hint}")
        else:
            lines.append(f"  - {r['name']:40s}  {r['verdict']:14s}  callers={r['caller_count']}")
    lines.append("")
    lines.append("자세히 보려면: python memory/audit_dead_state.py --json")
    lines.append("(wildcard SELECT 표시: 컬럼이 SELECT * 로 read 될 가능성. 명시적 read 가 0 이라도 wildcard 가 있으면 dead 판정 신뢰도 ↓)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--silent", action="store_true",
                    help="dead 0건이면 출력 없음 (hook 용)")
    args = ap.parse_args()

    try:
        report = run_audit()
    except Exception as e:
        print(f"[ERR] audit failed: {e}", file=sys.stderr)
        return 2

    has_dead = report["summary"]["alive"] != report["summary"]["total"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.silent and not has_dead:
        pass
    else:
        print(format_human(report))

    return 1 if has_dead else 0


if __name__ == "__main__":
    sys.exit(main())
