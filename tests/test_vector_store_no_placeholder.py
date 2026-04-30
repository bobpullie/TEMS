"""Critical #4 regression: vector_store.upsert must not create placeholder rows."""
import sqlite3

from tems.fts5_memory import MemoryDB
from tems.vector_store import VectorStore


def test_upsert_skips_when_rule_absent(tmp_path, capsys):
    """rule_id 가 memory_logs 에 없으면 upsert 가 placeholder 를 생성하지 않고
    skip 해야 한다 (이전 구현은 빈 row 를 INSERT 했다).
    """
    db_path = tmp_path / "novec.db"
    MemoryDB(str(db_path))  # schema 초기화 (memory_logs + embedding_meta)
    vs = VectorStore(str(db_path))

    # 룰을 commit_memory 없이 곧장 upsert — 이전 구현은 placeholder 생성
    vs.upsert(rule_id=9999, vec=[0.1] * 4, model_id="test-model")

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT id FROM memory_logs WHERE id = 9999"
        ).fetchall()
        meta_rows = conn.execute(
            "SELECT rule_id FROM embedding_meta WHERE rule_id = 9999"
        ).fetchall()

    # placeholder 가 만들어지지 않았어야 함
    assert rows == [], f"phantom placeholder 발견: {rows}"
    # embedding_meta 도 추가되지 않았어야 함 (cascade — 룰 없으면 메타도 없음)
    assert meta_rows == [], f"orphan embedding_meta 발견: {meta_rows}"

    # stderr 에 진단 경고 (silent skip 방지)
    captured = capsys.readouterr()
    assert "skip upsert" in captured.err
    assert "9999" in captured.err


def test_upsert_works_when_rule_exists(tmp_path):
    """정상 경로 — 룰이 존재하면 embedding 이 정상 저장돼야 한다 (회귀 방지)."""
    db_path = tmp_path / "yesvec.db"
    db = MemoryDB(str(db_path))
    rid = db.commit_memory(
        context_tags=["test"],
        action_taken="action",
        result="result",
        correction_rule="rule with embedding",
        keyword_trigger="kw",
    )

    vs = VectorStore(str(db_path))
    vec = [0.2, 0.3, 0.4, 0.5]
    vs.upsert(rule_id=rid, vec=vec, model_id="test-model")

    with sqlite3.connect(str(db_path)) as conn:
        emb = conn.execute(
            "SELECT embedding FROM memory_logs WHERE id = ?", (rid,)
        ).fetchone()[0]
        meta = conn.execute(
            "SELECT model_id, dim FROM embedding_meta WHERE rule_id = ?",
            (rid,),
        ).fetchone()

    assert emb is not None, "embedding 이 저장되지 않음"
    assert meta == ("test-model", 4)


def test_upsert_idempotent(tmp_path):
    """같은 rule_id 에 두 번 upsert 해도 안전 (UPDATE 로 덮어쓰기)."""
    db_path = tmp_path / "idem.db"
    db = MemoryDB(str(db_path))
    rid = db.commit_memory(
        context_tags=["test"],
        action_taken="a",
        result="r",
        correction_rule="rule",
        keyword_trigger="kw",
    )

    vs = VectorStore(str(db_path))
    vs.upsert(rid, [0.1, 0.2, 0.3, 0.4], "model-v1")
    vs.upsert(rid, [0.5, 0.6, 0.7, 0.8], "model-v2")

    with sqlite3.connect(str(db_path)) as conn:
        meta = conn.execute(
            "SELECT model_id FROM embedding_meta WHERE rule_id = ?", (rid,)
        ).fetchone()
        # memory_logs 행 카운트 — placeholder 누적 없는지
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_logs WHERE id = ?", (rid,)
        ).fetchone()[0]

    assert meta == ("model-v2",), "두 번째 upsert 가 model_id 갱신 못함"
    assert count == 1, "동일 rule_id 에 row 가 여러 개 — placeholder 회귀"
