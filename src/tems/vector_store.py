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
        """embedding BLOB 컬럼 + embedding_meta 테이블 idempotent 생성."""
        with self._conn() as conn:
            # memory_logs가 없는 standalone DB(테스트 등)에도 동작하도록 최소 스키마 보장
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    context_tags TEXT NOT NULL DEFAULT '',
                    keyword_trigger TEXT DEFAULT '',
                    action_taken TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '',
                    correction_rule TEXT,
                    category TEXT DEFAULT 'general',
                    severity TEXT DEFAULT 'info',
                    created_at TEXT DEFAULT (datetime('now')),
                    embedding BLOB DEFAULT NULL
                )
            """)

            # memory_logs.embedding 컬럼 추가 (이미 있으면 무시)
            try:
                conn.execute(
                    "ALTER TABLE memory_logs ADD COLUMN embedding BLOB DEFAULT NULL"
                )
            except sqlite3.OperationalError:
                pass  # 이미 존재 — idempotent

            # embedding_meta 테이블 생성
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embedding_meta (
                    rule_id INTEGER PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (rule_id) REFERENCES memory_logs(id) ON DELETE CASCADE
                )
            """)
            conn.commit()

    def upsert(self, rule_id: int, vec: list[float], model_id: str) -> None:
        """rule_id의 임베딩을 저장/갱신 (idempotent).

        memory_logs 행이 없으면 최소 플레이스홀더를 INSERT한 뒤 embedding 갱신.
        이는 standalone DB(테스트) 또는 향후 race condition 방지를 위함.
        """
        blob = _pack_vec(vec)
        dim = len(vec)

        with self._conn() as conn:
            # 행이 있으면 UPDATE, 없으면 INSERT OR IGNORE 후 UPDATE
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_logs (id, timestamp, context_tags, action_taken, result)
                VALUES (?, datetime('now'), '', '', '')
                """,
                (rule_id,),
            )
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
