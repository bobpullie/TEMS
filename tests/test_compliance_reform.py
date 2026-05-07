"""S60 compliance reform — 4종 검증.

1. compute_ths active/passive 가중치
2. compliance_tracker Tier 1 scope gate (tool_pattern 미매칭 시 relevance_skipped)
3. decay --penalize-uncited (fire 누적 + active=0 → ths neutral 회귀)
4. retrospective_hook citation parser (TGL/TCL #N 탐지)
"""
from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest


# ─── 공용 fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_agent(tmp_path, monkeypatch):
    """canonical scaffold 미실행 환경에서 templates 를 직접 로드해 테스트.

    - .claude/tems_agent_id 마커
    - memory/ 디렉토리 + error_logs.db 스키마 적용
    - 템플릿 *.py 를 tmp_path/memory/ 로 복사 (각 테스트가 import 하기 위함)
    """
    base = tmp_path / "agent"
    (base / ".claude").mkdir(parents=True)
    (base / ".claude" / "tems_agent_id").write_text("test", encoding="utf-8")
    mem = base / "memory"
    mem.mkdir()

    # canonical schema 적용
    db_path = mem / "error_logs.db"
    conn = sqlite3.connect(str(db_path))
    from tems.schema import apply_schema
    apply_schema(conn)
    conn.commit()
    conn.close()

    # decay/compliance_tracker 템플릿 복사 (테스트 시 모듈로 import)
    src_root = Path(__file__).resolve().parents[1] / "src" / "tems" / "templates"
    for fname in ("decay.py", "compliance_tracker.py", "retrospective_hook.py"):
        shutil.copy2(src_root / fname, mem / fname)

    monkeypatch.setenv("TEMS_AGENT_ROOT", str(base))
    monkeypatch.syspath_prepend(str(mem))

    yield base, db_path

    # 모듈 캐시 청소
    for name in list(sys.modules):
        if name in {"decay", "compliance_tracker", "retrospective_hook"}:
            del sys.modules[name]


# ─── 1. compute_ths 가중치 ───────────────────────────────────────────────────

def test_compute_ths_active_dominates_passive(tmp_agent):
    base, db = tmp_agent
    decay = importlib.import_module("decay")

    # 동일 fire/c — active 1 vs passive 1 비교
    only_passive = decay.compute_ths(fire_count=10, compliance_count=1, violation_count=0, active_compliance_count=0)
    only_active  = decay.compute_ths(fire_count=10, compliance_count=0, violation_count=0, active_compliance_count=1)

    assert only_active > only_passive, (
        f"active 신호 가중치 {decay.THS_ACTIVE_WEIGHT} 가 passive {decay.THS_PASSIVE_WEIGHT} 보다 ths 를 더 끌어올려야 함. "
        f"got passive={only_passive:.3f} vs active={only_active:.3f}"
    )


def test_compute_ths_passive_only_below_full(tmp_agent):
    """passive 만 누적된 룰의 ths 가 1.0 에 도달하지 않아야 함 (S60 핵심)."""
    base, db = tmp_agent
    decay = importlib.import_module("decay")
    # fire 100 + passive 100 (극단) — 이전 공식이라면 1.0 도달
    ths = decay.compute_ths(fire_count=100, compliance_count=100, violation_count=0, active_compliance_count=0)
    assert ths < 0.95, f"passive-only 룰은 ths 가 0.95 미만이어야 (got {ths:.3f})"


def test_compute_ths_active_can_reach_full(tmp_agent):
    """active 인용 누적은 ths 1.0 도달 가능 — 진짜 적중 룰 우선 부상."""
    base, db = tmp_agent
    decay = importlib.import_module("decay")
    ths = decay.compute_ths(fire_count=20, compliance_count=0, violation_count=0, active_compliance_count=20)
    assert ths >= 0.99


# ─── 2. compliance_tracker Tier 1 scope gate ─────────────────────────────────

def test_scope_gate_relevance_skipped_when_tool_pattern_never_matches(tmp_agent):
    """tool_pattern 보유 가드가 윈도우 동안 자기 도구 0회 매칭 → relevance_skipped."""
    base, db = tmp_agent
    ct = importlib.import_module("compliance_tracker")

    # active_guards.json 작성 — tool_pattern 은 git push 한정
    guards_path = base / "memory" / "active_guards.json"
    guards_path.write_text(json.dumps({
        "guards": [{
            "rule_id": 999,
            "tool_pattern": r"git\s+push",
            "remaining_checks": 1,  # 다음 호출에서 만료
            "fired_at": "2026-05-07T10:00:00",
        }]
    }), encoding="utf-8")

    # memory_logs 에 더미 룰 등록
    conn = sqlite3.connect(str(db))
    conn.execute("""
        INSERT INTO memory_logs (id, timestamp, correction_rule, category, severity, context_tags)
        VALUES (999, ?, 'dummy', 'TGL', 'info', 'test')
    """, ("2026-05-07T09:00:00",))
    conn.commit()
    conn.close()

    # PostToolUse 시뮬: Edit 호출 — tool_pattern 'git push' 와 매칭 안 됨
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "foo.py", "old_string": "x", "new_string": "y"},
        "tool_response": "",
    }
    monkeystdin = json.dumps(payload)

    import io
    real_stdin = sys.stdin
    sys.stdin = io.StringIO(monkeystdin)
    try:
        try:
            ct.main()
        except SystemExit:
            pass
    finally:
        sys.stdin = real_stdin

    conn = sqlite3.connect(str(db))
    row = conn.execute("""
        SELECT compliance_count, relevance_skipped_count
        FROM rule_health WHERE rule_id = 999
    """).fetchone()
    conn.close()

    assert row is not None, "rule_health row 가 생성되어야 함"
    compliance_count, relevance_skipped = row
    assert (relevance_skipped or 0) == 1, f"relevance_skipped 1 기대, got {relevance_skipped}"
    assert (compliance_count or 0) == 0, f"compliance_count 는 증가 안 해야 (got {compliance_count})"


# ─── 3. decay --penalize-uncited ─────────────────────────────────────────────

def test_penalize_uncited_resets_ghost_ths(tmp_agent):
    """fire 충분 + active=0 + ths>0.5 → ths neutral (0.5) 강제 회귀."""
    base, db = tmp_agent
    decay = importlib.import_module("decay")

    conn = sqlite3.connect(str(db))
    # 케이스 1: 유령 룰 (penalize 대상)
    conn.execute("""INSERT INTO memory_logs (id, timestamp, correction_rule, category, severity, context_tags)
                    VALUES (1, ?, 'g', 'TGL', 'info', 't')""", ("2026-05-01",))
    conn.execute("""INSERT INTO rule_health (rule_id, fire_count, compliance_count, violation_count,
                                              active_compliance_count, ths_score, status)
                    VALUES (1, 50, 30, 0, 0, 0.95, 'warm')""")
    # 케이스 2: 진짜 적중 룰 (penalize 비대상 — active 있음)
    conn.execute("""INSERT INTO memory_logs (id, timestamp, correction_rule, category, severity, context_tags)
                    VALUES (2, ?, 'g', 'TGL', 'info', 't')""", ("2026-05-01",))
    conn.execute("""INSERT INTO rule_health (rule_id, fire_count, compliance_count, violation_count,
                                              active_compliance_count, ths_score, status)
                    VALUES (2, 30, 10, 0, 5, 0.92, 'warm')""")
    # 케이스 3: 저-fire 룰 (penalize 비대상 — fire 임계 미만)
    conn.execute("""INSERT INTO memory_logs (id, timestamp, correction_rule, category, severity, context_tags)
                    VALUES (3, ?, 'g', 'TGL', 'info', 't')""", ("2026-05-01",))
    conn.execute("""INSERT INTO rule_health (rule_id, fire_count, compliance_count, violation_count,
                                              active_compliance_count, ths_score, status)
                    VALUES (3, 5, 5, 0, 0, 0.65, 'warm')""")
    conn.commit()
    conn.close()

    # decay 모듈의 DB_PATH 를 tmp DB 로 강제
    decay.DB_PATH = db

    result = decay.penalize_uncited(dry_run=False)
    assert result["ok"]
    assert result["penalized"] == 1, f"1건만 페널티 (got {result['penalized']})"

    conn = sqlite3.connect(str(db))
    rows = {r[0]: r[1] for r in conn.execute("SELECT rule_id, ths_score FROM rule_health").fetchall()}
    conn.close()
    assert abs(rows[1] - 0.5) < 1e-6, f"#1 ths 0.5 회귀 기대 (got {rows[1]})"
    assert rows[2] > 0.9, f"#2 ths 보존 (got {rows[2]})"
    assert abs(rows[3] - 0.65) < 1e-6, f"#3 ths 보존 (got {rows[3]})"


# ─── 4. retrospective_hook citation parser ───────────────────────────────────

def test_citation_parser_extracts_rule_ids(tmp_agent):
    base, db = tmp_agent
    rh = importlib.import_module("retrospective_hook")

    cases = [
        ("TGL #54 에 따라 useRef 사용", {54}),
        ("TGL #54 와 TCL #66 적용", {54, 66}),
        ("주입된 TGL: #38", {38}),
        ("TGL#92, TGL #96", {92, 96}),
        ("그냥 텍스트 #54 인용 없음", set()),  # TGL/TCL 접두어 없음
        ("TGL 100", {100}),  # 공백 + # 없는 변형
    ]
    for text, expected in cases:
        got = rh.parse_citations(text)
        assert got == expected, f"input={text!r}: expected {expected}, got {got}"


def test_citation_parser_updates_active_compliance(tmp_agent):
    """parse_citations 결과가 rule_health.active_compliance_count 에 누적."""
    base, db = tmp_agent
    rh = importlib.import_module("retrospective_hook")
    rh.DB_PATH = db

    # 룰 행 미리 생성
    conn = sqlite3.connect(str(db))
    for rid in (10, 20):
        conn.execute("""INSERT INTO memory_logs (id, timestamp, correction_rule, category, severity, context_tags)
                        VALUES (?, ?, 'g', 'TGL', 'info', 't')""", (rid, "2026-05-01"))
    conn.commit()
    conn.close()

    n = rh.update_active_compliance({10, 20})
    assert n == 2

    conn = sqlite3.connect(str(db))
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT rule_id, active_compliance_count FROM rule_health"
    ).fetchall()}
    conn.close()
    assert rows[10] == 1
    assert rows[20] == 1

    # 두 번째 호출 — 누적 확인
    rh.update_active_compliance({10})
    conn = sqlite3.connect(str(db))
    val = conn.execute(
        "SELECT active_compliance_count FROM rule_health WHERE rule_id=10"
    ).fetchone()[0]
    conn.close()
    assert val == 2
