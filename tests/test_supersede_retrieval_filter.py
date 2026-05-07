"""S56-B: HybridRetriever.preflight() 가 superseded 규칙을 retrieval 단계에서 제외하는지 검증.

Regression: TemporalGraph.supersede_rule() 로 valid_until 이 설정된 규칙이
preflight 결과에 그대로 노출되어, 새 규칙(superseded_by 가 가리키는) 과 함께
이중 주입되던 문제. 본 테스트는 supersede 후 옛 규칙이 retrieval 단계에서 사라지는지 확인.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from tems.fts5_memory import MemoryDB
from tems.tems_engine import HybridRetriever, TemporalGraph


@pytest.fixture()
def tmp_db(monkeypatch):
    base = Path(__file__).resolve().parents[1] / "memory" / "_test_supersede_tmp"
    work = base / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "error_logs.db"
    db = MemoryDB(db_path=str(db_path))
    yield db
    shutil.rmtree(work, ignore_errors=True)


def _commit_tgl(db, rule_text, trigger):
    return db.commit_memory(
        context_tags=["test"],
        action_taken="[TGL] test",
        result="active",
        correction_rule=rule_text,
        keyword_trigger=trigger,
        category="TGL",
        severity="info",
    )


def test_superseded_rule_excluded_from_preflight(tmp_db):
    """supersede 후 옛 규칙은 preflight 결과에 미출현, 새 규칙만 노출"""
    old_id = _commit_tgl(
        tmp_db,
        "복원은 deprecated_path/rebuild.py 사용",
        "rebuild deprecated_path",
    )
    new_id = _commit_tgl(
        tmp_db,
        "복원은 canonical CLI 'tems restore --agent-id <id>' 사용",
        "rebuild deprecated_path tems_restore canonical",
    )

    # supersede 적용
    tg = TemporalGraph(db=tmp_db)
    assert tg.supersede_rule(old_id, new_id, "test supersede") is True

    # preflight 결과 검증
    h = HybridRetriever(db=tmp_db)
    res = h.preflight("rebuild deprecated_path", limit=10)
    ids = {r["id"] for r in res["tgl_hits"]}
    assert old_id not in ids, f"superseded rule {old_id} leaked into preflight: {ids}"
    assert new_id in ids, f"successor rule {new_id} missing from preflight: {ids}"


def test_search_returns_temporal_columns(tmp_db):
    """MemoryDB.search() 가 valid_until/superseded_by 컬럼을 반환해야 retrieval 필터가 작동"""
    rid = _commit_tgl(tmp_db, "active rule with sentinel_keyword_xyz", "sentinel_keyword_xyz")
    rows = tmp_db.search("sentinel_keyword_xyz", limit=5)
    assert rows
    assert "valid_until" in rows[0]
    assert "superseded_by" in rows[0]


def test_memorydb_preflight_excludes_superseded(tmp_db):
    """MemoryDB.preflight() (preflight_hook.py 의 1차 retrieval path) 도 superseded 규칙 제외.

    회귀 발견 컨텍스트: HybridRetriever.preflight 만 필터하던 초기 패치는 preflight_hook 의
    primary BM25 path (db.preflight) 를 우회 → 옛 규칙이 그대로 주입되던 문제.
    """
    old_id = _commit_tgl(
        tmp_db,
        "복원은 deprecated_path/legacy.py 사용",
        "rebuild deprecated_legacy",
    )
    new_id = _commit_tgl(
        tmp_db,
        "복원은 canonical CLI 'tems restore' 사용",
        "rebuild deprecated_legacy canonical_cli",
    )

    from tems.tems_engine import TemporalGraph
    tg = TemporalGraph(db=tmp_db)
    assert tg.supersede_rule(old_id, new_id, "test") is True

    res = tmp_db.preflight("rebuild deprecated_legacy", limit=10)
    ids = {r["id"] for r in res["tgl_hits"]}
    assert old_id not in ids, f"superseded rule {old_id} leaked into MemoryDB.preflight: {ids}"
    assert new_id in ids, f"successor rule {new_id} missing: {ids}"
