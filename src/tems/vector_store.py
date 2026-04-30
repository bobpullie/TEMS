"""
TEMS VectorStore — SQLite BLOB 벡터 저장 + 코사인 검색
=======================================================
외부 의존성 0: sqlite3 + struct + math만 사용.
float32 little-endian BLOB (768d=3072B, 1024d=4096B).
1000 규칙까지 전체 스캔 코사인 < 100ms 검증됨.
"""

import math
import sqlite3
import struct
from pathlib import Path
from typing import Optional


def _pack_vec(vec: list[float]) -> bytes:
    """float32 little-endian BLOB으로 직렬화."""
    n = len(vec)
    return struct.pack(f"<{n}f", *vec)


def _unpack_vec(blob: bytes) -> list[float]:
    """BLOB → float32 리스트 역직렬화."""
    n = len(blob) // 4  # float32 = 4 bytes
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    """코사인 유사도 (numpy 없이)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class VectorStore:
    """memory_logs.embedding BLOB 컬럼 + embedding_meta 테이블 관리.

    - upsert: rule_id → vec BLOB + model_id 메타 저장
    - search: query_vec와 코사인 전체 스캔 → top-k
    - needs_reindex: 현재 model_id와 불일치하는 rule_id 목록
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        from tems.schema import apply_schema
        with self._conn() as conn:
            apply_schema(conn)
            conn.commit()

    def upsert(self, rule_id: int, vec: list[float], model_id: str) -> None:
        """rule_id의 임베딩을 저장/갱신 (idempotent).

        Critical #4 fix: 이전 구현은 memory_logs 행이 없을 때 빈 placeholder
        row 를 INSERT OR IGNORE 했다. 이는 다음 부작용을 만들었다:
          - COUNT(*) 가 phantom row 만큼 부풀려져 lifecycle 통계 오염
          - FTS 트리거가 빈 placeholder 도 색인 → 검색 노이즈 + 후처리 비용
          - embedding_meta 와 memory_logs 의 ID 정합 책임이 vector_store 에 묻힘

        새 contract: 호출자가 memory_logs 에 룰을 먼저 commit 해야 한다.
        rule_id 가 존재하지 않으면 stderr 경고 + 조용히 skip (raise 는 기존
        호출자 호환성 유지를 위해 지양). 정상 경로 (commit_memory → embed → upsert)
        는 동작 동일 — placeholder 생성만 사라짐.
        """
        blob = _pack_vec(vec)
        dim = len(vec)

        with self._conn() as conn:
            # Precondition — 룰이 memory_logs 에 실제로 존재하는지 확인
            exists = conn.execute(
                "SELECT 1 FROM memory_logs WHERE id = ? LIMIT 1",
                (rule_id,),
            ).fetchone()
            if not exists:
                import sys
                print(
                    f"[vector_store] WARN: skip upsert(rule_id={rule_id}) — "
                    f"memory_logs 에 해당 row 없음. commit_memory() 를 먼저 호출하세요.",
                    file=sys.stderr,
                )
                return

            conn.execute(
                "UPDATE memory_logs SET embedding = ? WHERE id = ?",
                (blob, rule_id),
            )
            # embedding_meta upsert
            conn.execute(
                """
                INSERT INTO embedding_meta (rule_id, model_id, dim)
                VALUES (?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    model_id = excluded.model_id,
                    dim = excluded.dim,
                    created_at = datetime('now')
                """,
                (rule_id, model_id, dim),
            )
            conn.commit()

    def search(self, query_vec: list[float], limit: int = 20) -> list[tuple[int, float]]:
        """전체 스캔 + 코사인 유사도 → top-k (rule_id, score) 반환.

        embedding이 NULL인 행은 건너뜀.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, embedding FROM memory_logs WHERE embedding IS NOT NULL"
            ).fetchall()

        results: list[tuple[int, float]] = []
        for row in rows:
            try:
                vec = _unpack_vec(row["embedding"])
                score = _cosine(query_vec, vec)
                results.append((row["id"], score))
            except Exception:
                continue

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def needs_reindex(self, model_id: str) -> list[int]:
        """현재 model_id와 다른 모델로 임베딩된 rule_id 목록.

        embedding_meta에 없는 rule_id도 포함 (임베딩이 없는 행).
        """
        with self._conn() as conn:
            # embedding_meta에 등록됐지만 model_id가 다른 것
            wrong_model = conn.execute(
                "SELECT rule_id FROM embedding_meta WHERE model_id != ?",
                (model_id,),
            ).fetchall()

            # memory_logs에 있지만 embedding_meta에 없는 것
            unindexed = conn.execute(
                """
                SELECT m.id FROM memory_logs m
                LEFT JOIN embedding_meta e ON m.id = e.rule_id
                WHERE e.rule_id IS NULL
                """
            ).fetchall()

        result_ids = [r["rule_id"] for r in wrong_model]
        result_ids += [r["id"] for r in unindexed]
        return list(set(result_ids))
