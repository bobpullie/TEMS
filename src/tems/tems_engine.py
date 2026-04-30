"""
TEMS — Topological Evolving Memory System
==========================================
자가진화 메모리 엔진.

Phase 1: Hybrid Sparse-Dense Retrieval (FTS5 BM25 + QMD Vector + RRF)
Phase 2: Topological Health Score (THS) + 규칙 상태 전이
Phase 3: Topological Anomaly Certificate (TAC)
Phase 4: Meta-rule self-modification (Godel Agent pattern)

Evolution 1: Rule Graph — 규칙 간 위상적 연결 + 캐스케이드 활성화
Evolution 2: Predictive TGL — 시간적 선행 패턴 학습 + 사전 경고
Evolution 3: Adaptive Trigger — 미매칭 학습 + 트리거 자동 확장
Evolution 4: Temporal Graph — Graphiti 기반 시간축 지식 그래프
"""

import json
import subprocess
import sqlite3
import shutil
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from math import log

from .fts5_memory import MemoryDB

DB_PATH = None  # 외부에서 주입
QMD_RULES_DIR = None  # 외부에서 주입

# Windows에서 npm 글로벌 바이너리는 .cmd wrapper가 필요
QMD_CMD = "qmd.cmd" if sys.platform == "win32" else "qmd"


# ═══════════════════════════════════════════════════════════
# Dense 백엔드 — 측정 기반 자동 감지 (v0.3)
# ═══════════════════════════════════════════════════════════

# 모듈 전역 — 프로세스 lifetime 캐시 (한 번 결정되면 재평가 안 함)
_DENSE_BACKEND: Optional["EmbeddingBackend"] = None  # type: ignore[name-defined]


def _check_dense_available() -> bool:
    """임베딩 백엔드 가용성을 1회 체크하고 프로세스 lifetime 동안 캐시.

    우선순위 (spec §2):
    1. TEMS_DENSE=0 env → 강제 disable (사용자 명시 폴백)
    2. TEMS_DENSE=1 env → detect_backend() 호출 후 결과 사용
    3. 미설정 → 자동 감지 (latency 측정 기반)

    RuntimeError는 절대 raise하지 않음 — 실패 시 False 반환 (BM25 폴백).
    """
    global _DENSE_BACKEND
    if _DENSE_BACKEND is not None:
        return True

    val = os.environ.get("TEMS_DENSE", "").strip()
    if val == "0":
        return False
    if val == "1":
        try:
            from .dense_backend import detect_backend
            _DENSE_BACKEND = detect_backend()
        except Exception:
            pass
        return _DENSE_BACKEND is not None

    # 자동 감지
    try:
        from .dense_backend import detect_backend
        _DENSE_BACKEND = detect_backend()
    except Exception:
        pass
    return _DENSE_BACKEND is not None


def get_dense_backend():
    """현재 캐시된 dense backend 인스턴스 반환 (None이면 미가용).

    `from tems_engine import _DENSE_BACKEND`는 import 시점 None을 캐시하므로
    외부 모듈은 반드시 이 getter를 통해 최신 값을 조회.
    """
    return _DENSE_BACKEND


# ═══════════════════════════════════════════════════════════
# Phase 1: Hybrid Retrieval with RRF
# ═══════════════════════════════════════════════════════════

class HybridRetriever:
    """FTS5 BM25 + QMD 벡터 검색을 RRF(Reciprocal Rank Fusion)로 결합"""

    RRF_K = 60  # RRF 상수 (표준값)

    def __init__(self, db: MemoryDB, collection: str = "tems-kjongil"):
        self.db = db
        self.collection = collection

    def search(self, query: str, limit: int = 10, mode: str = "auto") -> list[dict]:
        """하이브리드 검색 수행.

        Args:
            query: 검색어
            limit: 최대 결과 수
            mode: "auto" | "sparse" | "dense" | "hybrid"
                  auto: 쿼리 특성에 따라 자동 가중치 조절 (Dynamic RRF)
        """
        # Sparse 검색 (FTS5 BM25)
        sparse_results = self._sparse_search(query, limit=limit * 2)

        # Dense 검색 (QMD Vector)
        dense_results = self._dense_search(query, limit=limit * 2)

        if mode == "sparse" or not dense_results:
            return sparse_results[:limit]
        if mode == "dense":
            return dense_results[:limit]

        # Dynamic RRF — 쿼리 특성에 따라 가중치 조절
        sparse_weight, dense_weight = self._compute_dynamic_weights(query)

        fused = self._reciprocal_rank_fusion(
            sparse_results, dense_results,
            sparse_weight=sparse_weight,
            dense_weight=dense_weight,
            limit=limit
        )
        return fused

    def preflight(self, query: str, limit: int = 5) -> dict:
        """하이브리드 preflight — TCL/TGL 분류 포함"""
        results = self.search(query, limit=limit * 3, mode="auto")
        return {
            "tcl_hits": [r for r in results if r.get("category") == "TCL"],
            "tgl_hits": [r for r in results if r.get("category") == "TGL"],
            "general_hits": [r for r in results if r.get("category") not in ("TCL", "TGL")],
        }

    def _sparse_search(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 BM25 검색"""
        try:
            return self.db.search(query, limit=limit)
        except Exception:
            return []

    def _dense_search(self, query: str, limit: int = 20) -> list[dict]:
        """LM Studio /v1/embeddings + SQLite BLOB 벡터 검색 (v0.3).

        qmd subprocess 의존 제거 — VectorStore 직접 사용.
        임베딩 서버 장애 시 빈 리스트 반환 (BM25 폴백 보장).
        """
        if not _check_dense_available():
            return []
        try:
            qv = _DENSE_BACKEND.embed(query)
            from .vector_store import VectorStore
            store = VectorStore(self.db.db_path)
            hits = store.search(qv, limit=limit)
            results = []
            for rule_id, score in hits:
                rule = self._load_rule_by_id(rule_id)
                if rule:
                    rule["source"] = "dense"
                    rule["dense_score"] = score
                    results.append(rule)
            return results
        except Exception:
            return []

    @staticmethod
    def _extract_rule_id(file_path: str) -> Optional[int]:
        """QMD 파일 경로에서 rule_id 추출.

        QMD는 가상 경로에서 언더스코어→하이픈 변환하므로 둘 다 처리:
        'qmd://tems-kjongil/rule_0001.md' → 1
        'qmd://tems-kjongil/rule-0001.md' → 1
        """
        try:
            filename = file_path.rsplit("/", 1)[-1]   # rule_0001.md or rule-0001.md
            stem = filename.replace(".md", "")          # rule_0001 or rule-0001
            # 언더스코어 또는 하이픈으로 분리
            for sep in ("_", "-"):
                if sep in stem:
                    parts = stem.split(sep)
                    # "rule" + "0001" 형태
                    if len(parts) >= 2 and parts[-1].isdigit():
                        return int(parts[-1])
            return None
        except (ValueError, IndexError):
            return None

    def _load_rule_by_id(self, rule_id: int) -> Optional[dict]:
        """SQLite DB에서 rule_id로 전체 규칙 데이터 로드"""
        try:
            with self.db._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM memory_logs WHERE id = ?", (rule_id,)
                ).fetchone()
                if row:
                    return dict(row)
        except Exception:
            pass
        return None

    def _compute_dynamic_weights(self, query: str) -> tuple[float, float]:
        """쿼리 특성에 따른 동적 가중치 계산 (Dynamic Weighted RRF).

        구체적/기술적 쿼리 → sparse(BM25) 가중치 ↑
        추상적/개념적 쿼리 → dense(벡터) 가중치 ↑

        v0.3: dense 메인, BM25 보강 (v0.2 반대)
        specificity=0 (추상): sparse=0.20, dense=0.80
        specificity=1 (구체): sparse=0.50, dense=0.50
        """
        specificity = self._query_specificity(query)
        sparse_w = 0.20 + 0.30 * specificity      # 0.20 ~ 0.50
        dense_w = 1.0 - sparse_w                   # 0.80 ~ 0.50
        return sparse_w, dense_w

    def _query_specificity(self, query: str) -> float:
        """쿼리의 구체성 점수 추정.

        구체적 신호: 에러코드, 파일명, 함수명, 영어 기술용어
        추상적 신호: 짧은 한국어, 의문문, 방향/전략 관련 어휘
        """
        tokens = query.split()
        if not tokens:
            return 0.5

        specific_signals = 0
        abstract_signals = 0

        for t in tokens:
            # 구체적 신호
            if any(c.isdigit() for c in t):
                specific_signals += 1
            if "." in t or "_" in t or "::" in t:
                specific_signals += 1
            if t.isupper() and len(t) >= 2:
                specific_signals += 1
            if any(t.lower().startswith(prefix) for prefix in
                   ["error", "fail", "cuda", "oom", "crash", "bug", "assert"]):
                specific_signals += 1

            # 추상적 신호
            if t in ("방향", "전략", "설계", "아키텍처", "방법", "접근", "개선",
                      "최적화", "어떻게", "왜", "무엇"):
                abstract_signals += 1

        total = specific_signals + abstract_signals
        if total == 0:
            return 0.5
        return specific_signals / total

    def _reciprocal_rank_fusion(
        self,
        sparse: list[dict],
        dense: list[dict],
        sparse_weight: float,
        dense_weight: float,
        limit: int
    ) -> list[dict]:
        """Reciprocal Rank Fusion으로 두 결과 목록을 합산"""
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        # Sparse 결과 스코어링
        for rank, item in enumerate(sparse):
            key = str(item.get("id", f"s_{rank}"))
            rrf_score = sparse_weight / (self.RRF_K + rank + 1)
            scores[key] = scores.get(key, 0) + rrf_score
            if key not in items:
                items[key] = item

        # Dense 결과 스코어링
        for rank, item in enumerate(dense):
            key = str(item.get("id", f"d_{rank}"))
            rrf_score = dense_weight / (self.RRF_K + rank + 1)
            scores[key] = scores.get(key, 0) + rrf_score
            if key not in items:
                items[key] = item

        # RRF 점수로 정렬
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [items[key] for key, _ in ranked[:limit]]


# ═══════════════════════════════════════════════════════════
# Phase 2: Topological Health Score (THS)
# ═══════════════════════════════════════════════════════════

class HealthScorer:
    """규칙의 위상적 건강도(THS)를 계산하고 상태 전이를 관리"""

    # THS 가중치 (Level 1 메타규칙 — Phase 4에서 자동 조절 대상)
    ALPHA = 0.25   # activation_frequency
    BETA = 0.30    # correction_impact
    GAMMA = 0.20   # topological_centrality
    DELTA = 0.10   # modification_entropy (감점)
    EPSILON = 0.15 # age_decay (감점)

    # 상태 전이 임계값
    HOT_THRESHOLD = 0.7
    WARM_THRESHOLD = 0.4
    COLD_DURATION_MONTHS = 6
    ARCHIVE_DURATION_MONTHS = 6

    def __init__(self, db: MemoryDB):
        self.db = db
        self._ensure_ths_table()

    def _ensure_ths_table(self):
        """THS 메타데이터 테이블 생성"""
        with self.db._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rule_health (
                    rule_id INTEGER PRIMARY KEY,
                    activation_count INTEGER DEFAULT 0,
                    correction_success INTEGER DEFAULT 0,
                    correction_total INTEGER DEFAULT 0,
                    modification_count INTEGER DEFAULT 0,
                    last_activated TEXT,
                    last_modified TEXT,
                    status TEXT DEFAULT 'warm',
                    status_changed_at TEXT DEFAULT (datetime('now')),
                    ths_score REAL DEFAULT 0.5,
                    ths_updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
                )
            """)
            conn.commit()

    def record_activation(self, rule_id: int, prevented_error: bool = False):
        """규칙이 트리거(활성화)되었을 때 기록"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            # upsert
            conn.execute("""
                INSERT INTO rule_health (rule_id, activation_count, correction_success,
                    correction_total, last_activated)
                VALUES (?, 1, ?, 1, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    activation_count = activation_count + 1,
                    correction_success = correction_success + ?,
                    correction_total = correction_total + 1,
                    last_activated = ?
            """, (rule_id, int(prevented_error), now, int(prevented_error), now))
            conn.commit()

    def record_modification(self, rule_id: int):
        """규칙이 수정되었을 때 기록"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            conn.execute("""
                INSERT INTO rule_health (rule_id, modification_count, last_modified)
                VALUES (?, 1, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    modification_count = modification_count + 1,
                    last_modified = ?
            """, (rule_id, now, now))
            conn.commit()

    def compute_ths(self, rule_id: int) -> float:
        """규칙의 Topological Health Score 계산.

        v0.4 정정 (input swap to alive sources):
        - act_freq: activation_count (record_activation dead) → fire_count (preflight 갱신)
        - 효용도: correction_success/total (dead) → compliance/violation 비율 (compliance_tracker)
        - age_decay: last_activated (dead) → last_fired (preflight 갱신) fallback chain

        산식 weight (ALPHA~EPSILON) 는 보존. input source 만 정정.
        """
        with self.db._conn() as conn:
            health = conn.execute(
                "SELECT * FROM rule_health WHERE rule_id = ?", (rule_id,)
            ).fetchone()
            rule = conn.execute(
                "SELECT * FROM memory_logs WHERE id = ?", (rule_id,)
            ).fetchone()

            if not rule:
                return 0.0

        if not health:
            # 건강 데이터 없으면 기본값
            return 0.5

        h = dict(health)

        # 1. Activation Frequency — fire_count 기반 (v0.4 정정)
        fire_count = h.get("fire_count") or 0
        act_freq = min(1.0, log(1 + fire_count) / log(1 + 50))

        # 2. Correction Impact — compliance/violation 비율 (v0.4 정정)
        comp = h.get("compliance_count") or 0
        viol = h.get("violation_count") or 0
        total_judged = comp + viol
        if total_judged > 0:
            corr_impact = comp / total_judged
        else:
            corr_impact = 0.5  # 판정 데이터 없으면 중립

        # 3. Topological Centrality (다른 규칙과의 키워드 겹침 정도)
        centrality = self._compute_centrality(rule_id)

        # 4. Modification Entropy (수정이 잦으면 불안정)
        mod_entropy = min(1.0, (h.get("modification_count") or 0) / 5.0)

        # 5. Age Decay — last_fired (preflight 갱신) fallback last_activated (v0.4 정정)
        last_activity = h.get("last_fired") or h.get("last_activated")
        age_decay = self._compute_age_decay(last_activity)

        ths = (
            self.ALPHA * act_freq
            + self.BETA * corr_impact
            + self.GAMMA * centrality
            - self.DELTA * mod_entropy
            - self.EPSILON * age_decay
        )
        ths = max(0.0, min(1.0, ths))

        # DB에 점수 저장
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            conn.execute(
                "UPDATE rule_health SET ths_score = ?, ths_updated_at = ? WHERE rule_id = ?",
                (ths, now, rule_id),
            )
            conn.commit()

        return ths

    def _compute_centrality(self, rule_id: int) -> float:
        """위상적 중심성: 이 규칙의 keyword_trigger가 다른 규칙들과 얼마나 겹치는가"""
        with self.db._conn() as conn:
            rule = conn.execute(
                "SELECT keyword_trigger FROM memory_logs WHERE id = ?", (rule_id,)
            ).fetchone()
            if not rule or not rule["keyword_trigger"]:
                return 0.0

            my_keywords = set(rule["keyword_trigger"].split())
            all_rules = conn.execute(
                "SELECT keyword_trigger FROM memory_logs WHERE id != ? AND keyword_trigger != ''",
                (rule_id,),
            ).fetchall()

            if not all_rules:
                return 0.0

            overlap_count = 0
            for other in all_rules:
                other_keywords = set(other["keyword_trigger"].split())
                if my_keywords & other_keywords:
                    overlap_count += 1

            return min(1.0, overlap_count / max(1, len(all_rules)))

    def _compute_age_decay(self, last_activated: Optional[str]) -> float:
        """마지막 활성화로부터의 시간 기반 감쇠 (0~1)"""
        if not last_activated:
            return 0.5  # 활성화 기록 없으면 중간값

        try:
            last = datetime.strptime(last_activated, "%Y-%m-%d %H:%M:%S")
            days_since = (datetime.now() - last).days
            # 270일(9개월)에서 1.0에 도달하는 선형 감쇠
            return min(1.0, days_since / 270.0)
        except ValueError:
            return 0.5

    def transition_status(self, rule_id: int) -> str:
        """THS에 따라 규칙 상태를 전이하고 새 상태를 반환"""
        ths = self.compute_ths(rule_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db._conn() as conn:
            current = conn.execute(
                "SELECT status, status_changed_at FROM rule_health WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()

            if not current:
                return "unknown"

            old_status = current["status"]
            new_status = old_status

            if ths >= self.HOT_THRESHOLD:
                new_status = "hot"
            elif ths >= self.WARM_THRESHOLD:
                new_status = "warm"
            else:
                # Cold 진입 조건: THS < WARM이 6개월 이상 유지
                if old_status == "warm":
                    new_status = "cold"
                elif old_status == "cold":
                    # Archive 진입 조건: Cold가 추가 6개월 유지
                    try:
                        changed = datetime.strptime(
                            current["status_changed_at"], "%Y-%m-%d %H:%M:%S"
                        )
                        months_in_cold = (datetime.now() - changed).days / 30
                        if months_in_cold >= self.ARCHIVE_DURATION_MONTHS:
                            new_status = "archive"
                    except ValueError:
                        pass

            # 상태 변경 시 업데이트
            if new_status != old_status:
                conn.execute(
                    "UPDATE rule_health SET status = ?, status_changed_at = ? WHERE rule_id = ?",
                    (new_status, now, rule_id),
                )
                conn.commit()

            return new_status

    def get_health_report(self) -> list[dict]:
        """전체 규칙의 건강 리포트"""
        with self.db._conn() as conn:
            rows = conn.execute("""
                SELECT m.id, m.category, m.correction_rule,
                       h.activation_count, h.modification_count,
                       h.ths_score, h.status, h.last_activated
                FROM memory_logs m
                LEFT JOIN rule_health h ON m.id = h.rule_id
                ORDER BY COALESCE(h.ths_score, 0.5) DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def run_lifecycle_sweep(self) -> dict:
        """전체 규칙에 대해 THS 재계산 + 상태 전이 수행"""
        with self.db._conn() as conn:
            rules = conn.execute("SELECT id FROM memory_logs").fetchall()

        transitions = {"hot": 0, "warm": 0, "cold": 0, "archive": 0, "reconstruct": 0}

        for rule in rules:
            rid = rule["id"]

            # 수정 3회 이상 → 재구성 큐
            with self.db._conn() as conn:
                health = conn.execute(
                    "SELECT modification_count FROM rule_health WHERE rule_id = ?",
                    (rid,),
                ).fetchone()

            if health and health["modification_count"] >= 3:
                transitions["reconstruct"] += 1
                continue

            new_status = self.transition_status(rid)
            transitions[new_status] = transitions.get(new_status, 0) + 1

        return transitions


# ═══════════════════════════════════════════════════════════
# Phase 3: Topological Anomaly Certificate (TAC)
# ═══════════════════════════════════════════════════════════

class AnomalyCertifier:
    """예외케이스의 위상적 분류 및 인증"""

    def __init__(self, db: MemoryDB):
        self.db = db
        self._ensure_exception_table()

    def _ensure_exception_table(self):
        """예외 관리 테이블 생성"""
        with self.db._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS exceptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER,
                    exception_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    occurrence_count INTEGER DEFAULT 1,
                    persistence_score REAL DEFAULT 0.0,
                    created_at TEXT DEFAULT (datetime('now')),
                    last_seen TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'active',
                    promoted_to_rule_id INTEGER,
                    FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
                )
            """)
            conn.commit()

    def classify_exception(
        self,
        description: str,
        related_rule_id: Optional[int] = None,
    ) -> dict:
        """예외를 Type A/B/C로 분류.

        Type A: 규칙 커버리지 부족 → 기존 규칙 확장 권고
        Type B: 규칙 간 충돌 → 우선순위 재조정 권고
        Type C: 진짜 이상치 → 독립 예외 규칙으로 저장
        """
        # 관련 규칙이 있는지 검색
        related = self.db.search(description, limit=5)

        if not related:
            # 매칭되는 규칙이 전혀 없음 → Type A (커버리지 부족)
            exc_type = "A"
            recommendation = "새 규칙 생성 또는 기존 규칙의 keyword_trigger 확장 필요"
        elif len(related) >= 2:
            # 여러 규칙이 매칭되나 correction_rule이 상충
            rules = [r["correction_rule"] for r in related[:3] if r.get("correction_rule")]
            if len(set(rules)) > 1:
                exc_type = "B"
                recommendation = "충돌 규칙 간 우선순위 재조정 필요"
            else:
                exc_type = "C"
                recommendation = "기존 규칙으로 포괄 불가 — 독립 예외 규칙으로 저장"
        else:
            exc_type = "C"
            recommendation = "기존 규칙으로 포괄 불가 — 독립 예외 규칙으로 저장"

        # DB에 기록
        with self.db._conn() as conn:
            # 동일 예외가 이미 있는지 확인
            existing = conn.execute(
                "SELECT id, occurrence_count FROM exceptions WHERE description = ?",
                (description,),
            ).fetchone()

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if existing:
                new_count = existing["occurrence_count"] + 1
                conn.execute(
                    "UPDATE exceptions SET occurrence_count = ?, last_seen = ? WHERE id = ?",
                    (new_count, now, existing["id"]),
                )
                exc_id = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO exceptions
                        (rule_id, exception_type, description)
                    VALUES (?, ?, ?)""",
                    (related_rule_id, exc_type, description),
                )
                exc_id = cursor.lastrowid

            conn.commit()

        return {
            "exception_id": exc_id,
            "type": exc_type,
            "recommendation": recommendation,
            "related_rules": [r["id"] for r in related[:3]],
        }

    def compute_persistence(self, exception_id: int) -> float:
        """Persistence Score 계산 — 예외의 지속성/중요도 판별.

        높은 점수 = 구조적 헛점 (genuine feature)
        낮은 점수 = 일시적 노이즈
        """
        with self.db._conn() as conn:
            exc = conn.execute(
                "SELECT * FROM exceptions WHERE id = ?", (exception_id,)
            ).fetchone()

            if not exc:
                return 0.0

        e = dict(exc)

        # 출현 빈도 (반복될수록 genuine)
        freq_score = min(1.0, log(1 + e["occurrence_count"]) / log(1 + 10))

        # 시간적 지속성 (오래 살아남을수록 genuine)
        try:
            created = datetime.strptime(e["created_at"], "%Y-%m-%d %H:%M:%S")
            last = datetime.strptime(e["last_seen"], "%Y-%m-%d %H:%M:%S")
            lifespan = (last - created).days
            time_score = min(1.0, lifespan / 90.0)  # 90일이면 1.0
        except ValueError:
            time_score = 0.0

        # 최근성 (최근에도 나타나면 active)
        try:
            last_seen = datetime.strptime(e["last_seen"], "%Y-%m-%d %H:%M:%S")
            days_ago = (datetime.now() - last_seen).days
            recency = max(0.0, 1.0 - days_ago / 270.0)
        except ValueError:
            recency = 0.0

        persistence = 0.4 * freq_score + 0.3 * time_score + 0.3 * recency

        # 점수 저장
        with self.db._conn() as conn:
            conn.execute(
                "UPDATE exceptions SET persistence_score = ? WHERE id = ?",
                (persistence, exception_id),
            )
            conn.commit()

        return persistence

    def promote_exception(self, exception_id: int) -> Optional[int]:
        """예외를 정식 규칙(TGL)으로 승격"""
        with self.db._conn() as conn:
            exc = conn.execute(
                "SELECT * FROM exceptions WHERE id = ?", (exception_id,)
            ).fetchone()

            if not exc:
                return None

        e = dict(exc)

        # TGL로 커밋
        new_rule_id = self.db.commit_tgl(
            error_description=f"[승격된 예외 #{e['id']}] {e['description']}",
            topological_case=f"반복 출현 예외 (Type {e['exception_type']}, {e['occurrence_count']}회)",
            guard_rule=e["description"],
            keyword_trigger=e["description"],  # 예외 설명 자체를 트리거로
            context_tags=["promoted_exception", f"type_{e['exception_type']}"],
            severity="warning",
        )

        # 예외 상태 업데이트
        with self.db._conn() as conn:
            conn.execute(
                "UPDATE exceptions SET status = 'promoted', promoted_to_rule_id = ? WHERE id = ?",
                (new_rule_id, exception_id),
            )
            conn.commit()

        return new_rule_id

    def run_exception_sweep(self, promote_threshold: float = 0.6) -> dict:
        """전체 예외에 대해 persistence 재계산 + 승격/만료 수행"""
        with self.db._conn() as conn:
            exceptions = conn.execute(
                "SELECT id FROM exceptions WHERE status = 'active'"
            ).fetchall()

        results = {"promoted": 0, "expired": 0, "active": 0}

        for exc in exceptions:
            eid = exc["id"]
            persistence = self.compute_persistence(eid)

            if persistence >= promote_threshold:
                self.promote_exception(eid)
                results["promoted"] += 1
            elif persistence < 0.1:
                # 매우 낮은 persistence → 만료
                with self.db._conn() as conn:
                    conn.execute(
                        "UPDATE exceptions SET status = 'expired' WHERE id = ?",
                        (eid,),
                    )
                    conn.commit()
                results["expired"] += 1
            else:
                results["active"] += 1

        return results


# ═══════════════════════════════════════════════════════════
# Phase 4: Meta-Rule Self-Modification (Godel Agent Pattern)
# ═══════════════════════════════════════════════════════════

class MetaRuleEngine:
    """메타규칙 자기 수정 엔진.

    Level 0: 구체적 규칙 (TCL/TGL) — MemoryDB에 저장
    Level 1: 규칙 평가 정책 (THS 가중치) — 이 클래스에서 관리
    Level 2: 정책 평가 기준 (시스템 건강도 메트릭)
    """

    def __init__(self, db: MemoryDB):
        self.db = db
        self.scorer = HealthScorer(db=self.db)
        self._ensure_meta_table()

    def _ensure_meta_table(self):
        """메타규칙 이력 테이블"""
        with self.db._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level INTEGER NOT NULL,
                    parameter_name TEXT NOT NULL,
                    old_value REAL,
                    new_value REAL,
                    reason TEXT,
                    system_health_before REAL,
                    system_health_after REAL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()

    def compute_system_health(self) -> dict:
        """Level 2: 시스템 전체 건강도 메트릭"""
        report = self.scorer.get_health_report()
        if not report:
            return {"overall": 0.5, "coverage": 0.0, "stability": 0.0, "freshness": 0.0}

        # 커버리지: hot+warm 비율
        active = sum(1 for r in report if r.get("status") in ("hot", "warm", None))
        coverage = active / len(report)

        # 안정성: 수정 빈도가 낮을수록 안정
        mod_counts = [r.get("modification_count", 0) or 0 for r in report]
        avg_mods = sum(mod_counts) / len(mod_counts) if mod_counts else 0
        stability = max(0.0, 1.0 - avg_mods / 5.0)

        # 신선도: 최근 활성화된 규칙 비율 (v0.4 정정: last_activated dead → last_fired alive)
        now = datetime.now()
        fresh = 0
        for r in report:
            last_activity = r.get("last_fired") or r.get("last_activated")
            if not last_activity:
                continue
            last = None
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    last = datetime.strptime(str(last_activity), fmt)
                    break
                except ValueError:
                    continue
            if last and (now - last).days < 30:
                fresh += 1
        freshness = fresh / len(report) if report else 0.0

        overall = 0.4 * coverage + 0.3 * stability + 0.3 * freshness

        return {
            "overall": round(overall, 3),
            "coverage": round(coverage, 3),
            "stability": round(stability, 3),
            "freshness": round(freshness, 3),
            "total_rules": len(report),
            "active_rules": active,
        }

    def suggest_weight_adjustment(self) -> Optional[dict]:
        """Level 1: 시스템 건강도에 따라 THS 가중치 조절 제안"""
        health = self.compute_system_health()

        suggestion = None

        if health["freshness"] < 0.3:
            # 규칙이 오래됨 → age_decay 가중치 높여서 순환 촉진
            suggestion = {
                "parameter": "EPSILON (age_decay)",
                "direction": "increase",
                "reason": f"freshness={health['freshness']:.2f} — 규칙 순환이 정체됨, age_decay 강화 권고",
                "current": self.scorer.EPSILON,
                "suggested": min(0.25, self.scorer.EPSILON + 0.05),
            }
        elif health["stability"] < 0.5:
            # 수정이 잦음 → modification_entropy 가중치 높여서 불안정 규칙 퇴출
            suggestion = {
                "parameter": "DELTA (modification_entropy)",
                "direction": "increase",
                "reason": f"stability={health['stability']:.2f} — 규칙 수정 빈도 높음, 불안정 규칙 퇴출 강화 권고",
                "current": self.scorer.DELTA,
                "suggested": min(0.20, self.scorer.DELTA + 0.05),
            }
        elif health["coverage"] < 0.5:
            # 활성 규칙 비율 낮음 → 임계값 낮춰서 더 많은 규칙 활성화
            suggestion = {
                "parameter": "WARM_THRESHOLD",
                "direction": "decrease",
                "reason": f"coverage={health['coverage']:.2f} — 활성 규칙 부족, 임계값 하향 권고",
                "current": self.scorer.WARM_THRESHOLD,
                "suggested": max(0.2, self.scorer.WARM_THRESHOLD - 0.1),
            }

        return suggestion


# ═══════════════════════════════════════════════════════════
# QMD 동기화: FTS5 규칙 → QMD 검색 가능 마크다운
# ═══════════════════════════════════════════════════════════

def sync_rules_to_qmd(db: MemoryDB, qmd_rules_dir: Path) -> int:
    """FTS5 DB의 활성 규칙들을 QMD 인덱싱 가능한 개별 마크다운 파일로 내보내기.

    각 규칙을 rule_{id:04d}.md 파일로 생성하여 QMD가 개별 벡터로 임베딩하도록 함.
    DB에 없는 stale 파일은 삭제.
    """
    rules = db.get_recent(200)

    qmd_rules_dir.mkdir(parents=True, exist_ok=True)

    active_ids = set()

    for r in rules:
        rule_id = r["id"]
        active_ids.add(rule_id)

        rule_file = qmd_rules_dir / f"rule_{rule_id:04d}.md"
        content = _format_rule_markdown(r)
        rule_file.write_text(content, encoding="utf-8")

    # stale 파일 정리: DB에 없는 rule_*.md 삭제
    for f in qmd_rules_dir.glob("rule_*.md"):
        try:
            file_id = int(f.stem.split("_")[1])
            if file_id not in active_ids:
                f.unlink()
        except (ValueError, IndexError):
            pass

    # QMD 인덱스 갱신
    try:
        subprocess.run(
            [QMD_CMD, "update"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    return len(rules)


def _format_rule_markdown(rule: dict) -> str:
    """규칙을 QMD 인덱싱에 최적화된 마크다운으로 포맷."""
    rid = rule["id"]
    cat = rule.get("category", "general")
    tags = rule.get("context_tags", "")
    trigger = rule.get("keyword_trigger", "")
    correction = rule.get("correction_rule", "")
    severity = rule.get("severity", "info")
    action = rule.get("action_taken", "")
    result = rule.get("result", "")

    lines = [
        f"---",
        f"rule_id: {rid}",
        f"category: {cat}",
        f"tags: {tags}",
        f"trigger: {trigger}",
        f"severity: {severity}",
        f"---",
        f"",
        f"# [{cat}] Rule #{rid}",
        f"",
        f"**Keywords:** {trigger}",
        f"",
        f"**Rule:** {correction}",
        f"",
        f"**Context:** {action}",
        f"",
        f"**Result:** {result}",
    ]
    return "\n".join(lines) + "\n"


def sync_single_rule_to_qmd(rule_id: int, db: MemoryDB, qmd_rules_dir: Path):
    """단일 규칙을 QMD 마크다운 파일로 내보내기 (tems_commit 후 호출).

    전체 재sync 없이 해당 규칙만 빠르게 갱신.
    """
    qmd_rules_dir.mkdir(parents=True, exist_ok=True)

    try:
        with db._conn() as conn:
            row = conn.execute(
                "SELECT * FROM memory_logs WHERE id = ?", (rule_id,)
            ).fetchone()
            if not row:
                return

            rule = dict(row)
            rule_file = qmd_rules_dir / f"rule_{rule_id:04d}.md"
            content = _format_rule_markdown(rule)
            rule_file.write_text(content, encoding="utf-8")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# Evolution 1: Rule Graph — 규칙 간 위상적 연결
# ═══════════════════════════════════════════════════════════

class RuleGraph:
    """규칙 간 위상적 연결 그래프.

    노드: 각 규칙 (memory_logs)
    엣지: keyword_trigger 겹침 (정적) + co-activation 패턴 (동적)

    캐스케이드: 규칙 A가 발동되면 연결된 규칙 B도 함께 주입
    """

    def __init__(self, db: MemoryDB):
        self.db = db
        self._ensure_graph_tables()

    def _ensure_graph_tables(self):
        """그래프 엣지 + 공동 활성화 이력 테이블"""
        with self.db._conn() as conn:
            # 규칙 간 엣지 (가중치 그래프)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rule_edges (
                    rule_a INTEGER NOT NULL,
                    rule_b INTEGER NOT NULL,
                    edge_type TEXT NOT NULL,
                    weight REAL DEFAULT 0.0,
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (rule_a, rule_b, edge_type)
                )
            """)

            # 공동 활성화 이력 (어떤 규칙들이 같은 프롬프트에서 함께 트리거되었는가)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS co_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_hash TEXT NOT NULL,
                    rule_ids TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()

    def build_keyword_edges(self):
        """keyword_trigger 겹침 기반으로 정적 엣지 구축"""
        with self.db._conn() as conn:
            rules = conn.execute(
                "SELECT id, keyword_trigger FROM memory_logs WHERE keyword_trigger != ''"
            ).fetchall()

        rule_keywords = {}
        for r in rules:
            rule_keywords[r["id"]] = set(r["keyword_trigger"].split())

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        edges_created = 0

        with self.db._conn() as conn:
            rule_ids = list(rule_keywords.keys())
            for i, rid_a in enumerate(rule_ids):
                for rid_b in rule_ids[i + 1:]:
                    kw_a = rule_keywords[rid_a]
                    kw_b = rule_keywords[rid_b]

                    if not kw_a or not kw_b:
                        continue

                    # Jaccard 유사도
                    intersection = kw_a & kw_b
                    union = kw_a | kw_b
                    jaccard = len(intersection) / len(union) if union else 0

                    if jaccard > 0.15:  # 15% 이상 겹침 시 연결
                        conn.execute("""
                            INSERT INTO rule_edges (rule_a, rule_b, edge_type, weight, updated_at)
                            VALUES (?, ?, 'keyword_overlap', ?, ?)
                            ON CONFLICT(rule_a, rule_b, edge_type) DO UPDATE SET
                                weight = ?, updated_at = ?
                        """, (rid_a, rid_b, jaccard, now, jaccard, now))
                        edges_created += 1

            conn.commit()
        return edges_created

    def record_co_activation(self, prompt: str, triggered_rule_ids: list[int]):
        """동일 프롬프트에서 함께 트리거된 규칙들을 기록 → co-activation 엣지 강화"""
        if len(triggered_rule_ids) < 2:
            return

        prompt_hash = str(hash(prompt))[:16]
        ids_str = ",".join(str(i) for i in sorted(triggered_rule_ids))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db._conn() as conn:
            conn.execute(
                "INSERT INTO co_activations (prompt_hash, rule_ids, created_at) VALUES (?, ?, ?)",
                (prompt_hash, ids_str, now),
            )

            # 모든 쌍에 대해 co-activation 엣지 강화
            for i, rid_a in enumerate(triggered_rule_ids):
                for rid_b in triggered_rule_ids[i + 1:]:
                    a, b = min(rid_a, rid_b), max(rid_a, rid_b)
                    conn.execute("""
                        INSERT INTO rule_edges (rule_a, rule_b, edge_type, weight, updated_at)
                        VALUES (?, ?, 'co_activation', 1.0, ?)
                        ON CONFLICT(rule_a, rule_b, edge_type) DO UPDATE SET
                            weight = weight + 1.0, updated_at = ?
                    """, (a, b, now, now))

            conn.commit()

    def get_cascade_rules(self, triggered_rule_ids: list[int], threshold: float = 0.3) -> list[dict]:
        """트리거된 규칙과 연결된 추가 규칙을 캐스케이드로 반환.

        직접 매칭된 규칙 외에, 그래프에서 연결 강도가 threshold 이상인 이웃 규칙도 포함.
        """
        if not triggered_rule_ids:
            return []

        cascade_ids = set()

        with self.db._conn() as conn:
            for rid in triggered_rule_ids:
                # 양방향 엣지 검색
                neighbors = conn.execute("""
                    SELECT rule_b as neighbor, SUM(weight) as total_weight
                    FROM rule_edges WHERE rule_a = ?
                    GROUP BY rule_b HAVING total_weight >= ?
                    UNION
                    SELECT rule_a as neighbor, SUM(weight) as total_weight
                    FROM rule_edges WHERE rule_b = ?
                    GROUP BY rule_a HAVING total_weight >= ?
                """, (rid, threshold, rid, threshold)).fetchall()

                for n in neighbors:
                    if n["neighbor"] not in triggered_rule_ids:
                        cascade_ids.add(n["neighbor"])

            if not cascade_ids:
                return []

            # 캐스케이드 규칙 정보 조회
            placeholders = ",".join("?" * len(cascade_ids))
            rows = conn.execute(
                f"SELECT * FROM memory_logs WHERE id IN ({placeholders})",
                list(cascade_ids),
            ).fetchall()

            return [dict(r) for r in rows]

    def get_graph_stats(self) -> dict:
        """그래프 통계"""
        with self.db._conn() as conn:
            total_edges = conn.execute("SELECT COUNT(*) FROM rule_edges").fetchone()[0]
            keyword_edges = conn.execute(
                "SELECT COUNT(*) FROM rule_edges WHERE edge_type = 'keyword_overlap'"
            ).fetchone()[0]
            coact_edges = conn.execute(
                "SELECT COUNT(*) FROM rule_edges WHERE edge_type = 'co_activation'"
            ).fetchone()[0]
            total_coacts = conn.execute(
                "SELECT COUNT(*) FROM co_activations"
            ).fetchone()[0]

        return {
            "total_edges": total_edges,
            "keyword_overlap_edges": keyword_edges,
            "co_activation_edges": coact_edges,
            "total_co_activation_events": total_coacts,
        }


# ═══════════════════════════════════════════════════════════
# Evolution 2: Predictive TGL — 시간적 선행 패턴 학습
# ═══════════════════════════════════════════════════════════

class PredictiveTGL:
    """TGL 간 시간적 선행 관계를 학습하여 사전 경고.

    에러 A 발생 후 에러 B가 자주 따라오는 패턴을 기록하고,
    다음에 A가 발생하면 B에 대해 사전 경고합니다.
    """

    SEQUENCE_WINDOW_HOURS = 24  # 24시간 이내의 연속 TGL을 시퀀스로 간주

    def __init__(self, db: MemoryDB):
        self.db = db
        self._ensure_sequence_table()

    def _ensure_sequence_table(self):
        """TGL 시퀀스 패턴 테이블"""
        with self.db._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tgl_sequences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    predecessor_id INTEGER NOT NULL,
                    successor_id INTEGER NOT NULL,
                    occurrence_count INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.0,
                    last_seen TEXT DEFAULT (datetime('now')),
                    UNIQUE(predecessor_id, successor_id)
                )
            """)
            conn.commit()

    def record_tgl_event(self, tgl_rule_id: int):
        """TGL이 발동될 때마다 호출. 이전 TGL과의 시퀀스 관계를 기록."""
        now = datetime.now()
        window_start = (now - timedelta(hours=self.SEQUENCE_WINDOW_HOURS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        with self.db._conn() as conn:
            # 최근 24시간 내 발동된 다른 TGL 조회
            recent_tgls = conn.execute("""
                SELECT DISTINCT m.id
                FROM memory_logs m
                JOIN rule_health h ON m.id = h.rule_id
                WHERE m.category = 'TGL'
                  AND m.id != ?
                  AND h.last_activated >= ?
                ORDER BY h.last_activated DESC
                LIMIT 5
            """, (tgl_rule_id, window_start)).fetchall()

            # 각 선행 TGL → 현재 TGL 시퀀스를 기록
            for pred in recent_tgls:
                pred_id = pred["id"]
                conn.execute("""
                    INSERT INTO tgl_sequences (predecessor_id, successor_id, last_seen)
                    VALUES (?, ?, ?)
                    ON CONFLICT(predecessor_id, successor_id) DO UPDATE SET
                        occurrence_count = occurrence_count + 1,
                        last_seen = ?
                """, (pred_id, tgl_rule_id, now_str, now_str))

            # 신뢰도 재계산: confidence = 이 쌍의 출현 / 선행자의 총 발동 횟수
            for pred in recent_tgls:
                pred_id = pred["id"]
                total_pred = conn.execute(
                    "SELECT SUM(occurrence_count) FROM tgl_sequences WHERE predecessor_id = ?",
                    (pred_id,),
                ).fetchone()[0] or 1

                conn.execute("""
                    UPDATE tgl_sequences
                    SET confidence = CAST(occurrence_count AS REAL) / ?
                    WHERE predecessor_id = ?
                """, (total_pred, pred_id))

            conn.commit()

    def predict_next_errors(self, current_tgl_id: int, min_confidence: float = 0.3) -> list[dict]:
        """현재 TGL 기반으로 후속 에러를 예측.

        Returns: 높은 확률로 뒤따를 TGL 규칙 목록
        """
        with self.db._conn() as conn:
            predictions = conn.execute("""
                SELECT s.successor_id, s.occurrence_count, s.confidence,
                       m.context_tags, m.correction_rule, m.keyword_trigger
                FROM tgl_sequences s
                JOIN memory_logs m ON s.successor_id = m.id
                WHERE s.predecessor_id = ?
                  AND s.confidence >= ?
                ORDER BY s.confidence DESC
                LIMIT 5
            """, (current_tgl_id, min_confidence)).fetchall()

            return [dict(p) for p in predictions]

    def get_all_patterns(self, min_occurrences: int = 2) -> list[dict]:
        """학습된 모든 시퀀스 패턴 조회"""
        with self.db._conn() as conn:
            patterns = conn.execute("""
                SELECT s.*,
                       p.correction_rule as pred_rule, p.context_tags as pred_tags,
                       su.correction_rule as succ_rule, su.context_tags as succ_tags
                FROM tgl_sequences s
                JOIN memory_logs p ON s.predecessor_id = p.id
                JOIN memory_logs su ON s.successor_id = su.id
                WHERE s.occurrence_count >= ?
                ORDER BY s.confidence DESC
            """, (min_occurrences,)).fetchall()
            return [dict(p) for p in patterns]


# ═══════════════════════════════════════════════════════════
# Evolution 3: Adaptive Trigger — 트리거 자동 확장
# ═══════════════════════════════════════════════════════════

class AdaptiveTrigger:
    """미매칭 학습을 통한 keyword_trigger 자동 확장.

    1. 프롬프트가 규칙에 매칭되지 않았으나 수동으로 해당 규칙이 적용된 경우를 기록
    2. 누락된 표현을 자동으로 keyword_trigger에 추가
    3. 이를 통해 트리거가 점점 더 넓은 표현을 포괄하도록 진화
    """

    def __init__(self, db: MemoryDB):
        self.db = db
        self._ensure_miss_table()

    def _ensure_miss_table(self):
        """미매칭 기록 테이블"""
        with self.db._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trigger_misses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_text TEXT NOT NULL,
                    missed_keywords TEXT NOT NULL,
                    should_have_matched_rule_id INTEGER NOT NULL,
                    was_expanded INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (should_have_matched_rule_id) REFERENCES memory_logs(id)
                )
            """)
            conn.commit()

    def record_miss(self, prompt: str, missed_keywords: list[str], rule_id: int):
        """매칭 실패 기록.

        수동으로 규칙을 적용했을 때, 해당 프롬프트에서 어떤 키워드가
        트리거에 없어서 매칭이 안 되었는지를 기록.
        """
        kw_str = " ".join(missed_keywords)
        with self.db._conn() as conn:
            conn.execute(
                "INSERT INTO trigger_misses (prompt_text, missed_keywords, should_have_matched_rule_id) VALUES (?, ?, ?)",
                (prompt, kw_str, rule_id),
            )
            conn.commit()

    def auto_expand_triggers(self, min_misses: int = 2) -> list[dict]:
        """미매칭이 N회 이상 누적된 키워드를 자동으로 trigger에 추가.

        Returns: 확장된 규칙 목록
        """
        expanded = []

        with self.db._conn() as conn:
            # 규칙별로 미매칭 키워드를 집계
            rules_with_misses = conn.execute("""
                SELECT should_have_matched_rule_id as rule_id,
                       GROUP_CONCAT(missed_keywords, ' ') as all_missed,
                       COUNT(*) as miss_count
                FROM trigger_misses
                WHERE was_expanded = 0
                GROUP BY should_have_matched_rule_id
                HAVING miss_count >= ?
            """, (min_misses,)).fetchall()

            for rm in rules_with_misses:
                rule_id = rm["rule_id"]
                all_missed = rm["all_missed"]

                # 미매칭 키워드 빈도 분석
                word_freq: dict[str, int] = {}
                for word in all_missed.split():
                    word = word.strip()
                    if len(word) > 1:
                        word_freq[word] = word_freq.get(word, 0) + 1

                # 2회 이상 등장한 키워드만 추가 (노이즈 필터)
                new_keywords = [w for w, c in word_freq.items() if c >= min_misses]

                if not new_keywords:
                    continue

                # 현재 trigger 조회
                rule = conn.execute(
                    "SELECT keyword_trigger FROM memory_logs WHERE id = ?",
                    (rule_id,),
                ).fetchone()

                if not rule:
                    continue

                current_trigger = rule["keyword_trigger"] or ""
                current_set = set(current_trigger.split())

                # 중복 제거 후 추가
                genuinely_new = [kw for kw in new_keywords if kw not in current_set]
                if not genuinely_new:
                    continue

                updated_trigger = current_trigger + " " + " ".join(genuinely_new)

                # keyword_trigger 업데이트
                conn.execute(
                    "UPDATE memory_logs SET keyword_trigger = ? WHERE id = ?",
                    (updated_trigger.strip(), rule_id),
                )

                # 미매칭 기록을 확장 완료로 표시
                conn.execute(
                    "UPDATE trigger_misses SET was_expanded = 1 WHERE should_have_matched_rule_id = ?",
                    (rule_id,),
                )

                expanded.append({
                    "rule_id": rule_id,
                    "added_keywords": genuinely_new,
                    "total_trigger_size": len(current_set) + len(genuinely_new),
                })

            conn.commit()

        # FTS5 인덱스 재구축 (trigger가 변경되었으므로)
        if expanded:
            self._rebuild_fts_index()

        return expanded

    def _rebuild_fts_index(self):
        """FTS5 인덱스를 재구축하여 변경된 keyword_trigger 반영"""
        with self.db._conn() as conn:
            # FTS5 content 테이블 전체 재색인
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            conn.commit()

    def get_miss_stats(self) -> dict:
        """미매칭 통계"""
        with self.db._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM trigger_misses").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM trigger_misses WHERE was_expanded = 0"
            ).fetchone()[0]
            expanded = conn.execute(
                "SELECT COUNT(*) FROM trigger_misses WHERE was_expanded = 1"
            ).fetchone()[0]

            top_missed_rules = conn.execute("""
                SELECT should_have_matched_rule_id as rule_id, COUNT(*) as cnt
                FROM trigger_misses WHERE was_expanded = 0
                GROUP BY should_have_matched_rule_id
                ORDER BY cnt DESC LIMIT 5
            """).fetchall()

            return {
                "total_misses": total,
                "pending_expansion": pending,
                "already_expanded": expanded,
                "top_missed_rules": [dict(r) for r in top_missed_rules],
            }


# ═══════════════════════════════════════════════════════════
# Evolution 4: Temporal Graph — Graphiti 기반 시간축 지식 그래프
# ═══════════════════════════════════════════════════════════

class TemporalGraph:
    """Graphiti 영감의 시간축 지식 그래프.

    핵심 개념 (Zep/Graphiti paper, arXiv:2501.13956):
    - Bi-temporal tracking: event_time(사실 발생 시점) vs ingestion_time(기록 시점)
    - Temporal validity: valid_from ~ valid_until 구간으로 규칙의 유효 기간 관리
    - Rule versioning: 규칙 수정 시 구 버전 보존, superseded_by로 계보 추적
    - Contradiction detection: 새 규칙이 기존 규칙과 충돌하는지 자동 감지
    - Point-in-time query: "특정 시점에 활성이던 규칙" 시간축 질의
    """

    def __init__(self, db: MemoryDB):
        self.db = db
        self._ensure_temporal_tables()

    def _ensure_temporal_tables(self):
        """시간축 테이블 및 컬럼 생성"""
        with self.db._conn() as conn:
            # memory_logs에 temporal 컬럼 추가 (마이그레이션)
            for col, default in [
                ("valid_from", "NULL"),
                ("valid_until", "NULL"),
                ("superseded_by", "NULL"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE memory_logs ADD COLUMN {col} TEXT DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass  # 이미 존재

            # rule_edges에도 temporal 컬럼 추가
            for col, default in [
                ("valid_from", "NULL"),
                ("valid_until", "NULL"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE rule_edges ADD COLUMN {col} TEXT DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass

            # 규칙 버전 이력 테이블
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rule_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    snapshot_json TEXT NOT NULL,
                    changed_fields TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
                )
            """)

            # valid_from이 NULL인 기존 규칙들을 created_at으로 백필
            conn.execute("""
                UPDATE memory_logs
                SET valid_from = created_at
                WHERE valid_from IS NULL AND created_at IS NOT NULL
            """)

            conn.commit()

    # ─── Rule Supersession (규칙 대체) ───

    def supersede_rule(
        self,
        old_rule_id: int,
        new_rule_id: int,
        reason: str = "",
    ) -> bool:
        """기존 규칙을 새 규칙으로 대체. 구 규칙은 삭제하지 않고 무효화.

        Graphiti 핵심: 정보가 변하면 old fact를 invalidate — delete 아닌 supersede.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db._conn() as conn:
            # 구 규칙 존재 확인
            old = conn.execute(
                "SELECT * FROM memory_logs WHERE id = ?", (old_rule_id,)
            ).fetchone()
            if not old:
                return False

            # 구 규칙 스냅샷 저장 (버전 이력)
            version_count = conn.execute(
                "SELECT COUNT(*) FROM rule_versions WHERE rule_id = ?",
                (old_rule_id,),
            ).fetchone()[0]

            conn.execute(
                """INSERT INTO rule_versions
                   (rule_id, version, snapshot_json, changed_fields, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    old_rule_id,
                    version_count + 1,
                    json.dumps(dict(old), ensure_ascii=False, default=str),
                    "superseded",
                    reason,
                    now,
                ),
            )

            # 구 규칙에 valid_until + superseded_by 기록
            conn.execute(
                """UPDATE memory_logs
                   SET valid_until = ?, superseded_by = ?
                   WHERE id = ?""",
                (now, new_rule_id, old_rule_id),
            )

            # 새 규칙의 valid_from 설정
            conn.execute(
                """UPDATE memory_logs
                   SET valid_from = ?
                   WHERE id = ? AND valid_from IS NULL""",
                (now, new_rule_id),
            )

            conn.commit()

        # v0.4 정정: record_modification wire — 구 rule 의 modification_count/last_modified 갱신.
        # 이전엔 caller 0 (dead method) 였음. supersede 가 진짜 user-driven edit path 라
        # 여기서 호출하면 modification_count 가 의미있게 누적됨.
        try:
            scorer = HealthScorer(db=self.db)
            scorer.record_modification(old_rule_id)
        except Exception:
            # fail-soft — supersede 자체는 성공
            pass

        return True

    # ─── Rule Versioning (버전 이력) ───

    def record_version(self, rule_id: int, changed_fields: str, reason: str = ""):
        """규칙 수정 시 현재 상태를 버전으로 기록 (수정 전에 호출)"""
        with self.db._conn() as conn:
            rule = conn.execute(
                "SELECT * FROM memory_logs WHERE id = ?", (rule_id,)
            ).fetchone()
            if not rule:
                return

            version_count = conn.execute(
                "SELECT COUNT(*) FROM rule_versions WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()[0]

            conn.execute(
                """INSERT INTO rule_versions
                   (rule_id, version, snapshot_json, changed_fields, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    rule_id,
                    version_count + 1,
                    json.dumps(dict(rule), ensure_ascii=False, default=str),
                    changed_fields,
                    reason,
                ),
            )
            conn.commit()

    def get_rule_timeline(self, rule_id: int) -> dict:
        """규칙의 전체 시간축 이력: 생성 → 수정들 → (대체)

        Returns:
            {
                "rule_id": int,
                "current": dict,         # 현재 상태
                "is_active": bool,        # valid_until이 NULL이면 활성
                "superseded_by": int|None,
                "versions": [dict, ...],  # 시간순 버전 이력
                "successors": [dict, ...] # 이 규칙을 대체한 규칙들의 체인
            }
        """
        with self.db._conn() as conn:
            current = conn.execute(
                "SELECT * FROM memory_logs WHERE id = ?", (rule_id,)
            ).fetchone()
            if not current:
                return {"rule_id": rule_id, "error": "not found"}

            current = dict(current)

            versions = conn.execute(
                """SELECT * FROM rule_versions
                   WHERE rule_id = ?
                   ORDER BY version ASC""",
                (rule_id,),
            ).fetchall()

            # 대체 체인 추적 (A → B → C)
            successors = []
            next_id = current.get("superseded_by")
            visited = set()
            while next_id and next_id not in visited:
                visited.add(next_id)
                succ = conn.execute(
                    "SELECT id, category, correction_rule, valid_from, valid_until, superseded_by FROM memory_logs WHERE id = ?",
                    (next_id,),
                ).fetchone()
                if not succ:
                    break
                successors.append(dict(succ))
                next_id = succ["superseded_by"]

        return {
            "rule_id": rule_id,
            "current": current,
            "is_active": current.get("valid_until") is None,
            "superseded_by": current.get("superseded_by"),
            "versions": [dict(v) for v in versions],
            "successors": successors,
        }

    # ─── Point-in-Time Query (시점 질의) ───

    def query_at_time(
        self,
        timestamp: str,
        query: str = "",
        category: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """특정 시점에 활성이던 규칙들을 검색.

        Graphiti 핵심: "What was true at time T?"
        valid_from <= T AND (valid_until IS NULL OR valid_until > T)

        Args:
            timestamp: ISO 형식 (예: "2026-03-24 15:00:00" 또는 "2026-03-24")
            query: FTS5 검색어 (빈 문자열이면 전체)
            category: TCL|TGL|session 등 필터
        """
        with self.db._conn() as conn:
            conditions = [
                "(valid_from IS NULL OR valid_from <= ?)",
                "(valid_until IS NULL OR valid_until > ?)",
            ]
            params = [timestamp, timestamp]

            if category:
                conditions.append("category = ?")
                params.append(category)

            where = " AND ".join(conditions)

            if query:
                # FTS5 매칭 + temporal 필터
                rows = conn.execute(
                    f"""SELECT m.*, rank
                        FROM memory_fts f
                        JOIN memory_logs m ON f.rowid = m.id
                        WHERE memory_fts MATCH ? AND {where}
                        ORDER BY rank
                        LIMIT ?""",
                    [query] + params + [limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT * FROM memory_logs
                        WHERE {where}
                        ORDER BY id DESC
                        LIMIT ?""",
                    params + [limit],
                ).fetchall()

            return [dict(r) for r in rows]

    def get_active_rules(self, category: Optional[str] = None) -> list[dict]:
        """현재 시점에서 유효한(supersede되지 않은) 규칙만 반환"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.query_at_time(now, category=category)

    # ─── Contradiction Detection (모순 감지) ───

    def detect_contradictions(
        self,
        new_correction_rule: str,
        new_context_tags: list[str],
        similarity_threshold: float = 0.4,
    ) -> list[dict]:
        """새 규칙이 기존 활성 규칙과 의미적으로 충돌하는지 감지.

        Graphiti 핵심: 새 fact가 들어오면 기존 fact와 충돌 여부를 검증.
        동일 context_tags를 공유하면서 correction_rule이 다른 경우 → 잠재적 모순.

        Returns:
            충돌 가능성이 있는 기존 규칙 목록 (빈 리스트면 충돌 없음)
        """
        tags_query = " OR ".join(f'"{tag}"' for tag in new_context_tags if tag.strip())
        if not tags_query:
            return []

        # 동일 태그를 가진 활성 규칙 검색
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            candidates = conn.execute(
                """SELECT m.*, rank FROM memory_fts f
                   JOIN memory_logs m ON f.rowid = m.id
                   WHERE memory_fts MATCH ?
                     AND (m.valid_until IS NULL OR m.valid_until > ?)
                     AND m.correction_rule IS NOT NULL
                     AND m.correction_rule != ''
                   ORDER BY rank
                   LIMIT 20""",
                (tags_query, now),
            ).fetchall()

        contradictions = []
        new_words = set(new_correction_rule.lower().split())

        for c in candidates:
            c = dict(c)
            existing_words = set((c.get("correction_rule") or "").lower().split())

            if not existing_words:
                continue

            # Jaccard 유사도: 높으면 같은 주제
            intersection = new_words & existing_words
            union = new_words | existing_words
            jaccard = len(intersection) / len(union) if union else 0

            if jaccard < similarity_threshold:
                continue

            # 부정어 패턴으로 모순 감지
            negation_indicators = [
                "금지", "하지마", "하지않", "사용하지", "안됨", "불가",
                "대신", "instead", "don't", "not", "never", "avoid",
                "제거", "삭제", "중단", "폐기",
            ]

            has_negation_conflict = False
            for neg in negation_indicators:
                in_new = neg in new_correction_rule.lower()
                in_old = neg in (c.get("correction_rule") or "").lower()
                if in_new != in_old:
                    has_negation_conflict = True
                    break

            if has_negation_conflict:
                c["conflict_type"] = "negation"
                c["jaccard_similarity"] = round(jaccard, 3)
                contradictions.append(c)
            elif jaccard > 0.6:
                # 높은 유사도지만 다른 규칙 → 잠재적 중복
                c["conflict_type"] = "potential_duplicate"
                c["jaccard_similarity"] = round(jaccard, 3)
                contradictions.append(c)

        return contradictions

    # ─── Temporal Edge Management ───

    def invalidate_edge(self, rule_a: int, rule_b: int, edge_type: str):
        """엣지를 무효화 (삭제가 아닌 valid_until 설정)"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            conn.execute(
                """UPDATE rule_edges
                   SET valid_until = ?
                   WHERE rule_a = ? AND rule_b = ? AND edge_type = ?
                     AND valid_until IS NULL""",
                (now, rule_a, rule_b, edge_type),
            )
            conn.commit()

    def get_active_edges(self, rule_id: int) -> list[dict]:
        """특정 규칙의 현재 유효한 엣지만 반환"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM rule_edges
                   WHERE (rule_a = ? OR rule_b = ?)
                     AND (valid_until IS NULL OR valid_until > ?)""",
                (rule_id, rule_id, now),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Temporal Stats ───

    def get_temporal_stats(self) -> dict:
        """시간축 통계 — 규칙의 temporal 상태 분포"""
        with self.db._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memory_logs").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM memory_logs WHERE valid_until IS NULL"
            ).fetchone()[0]
            superseded = conn.execute(
                "SELECT COUNT(*) FROM memory_logs WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            versioned = conn.execute(
                "SELECT COUNT(DISTINCT rule_id) FROM rule_versions"
            ).fetchone()[0]
            total_versions = conn.execute(
                "SELECT COUNT(*) FROM rule_versions"
            ).fetchone()[0]

            # 평균 규칙 수명 (대체된 규칙들의 valid_from → valid_until 평균)
            avg_lifespan = conn.execute(
                """SELECT AVG(
                       julianday(valid_until) - julianday(valid_from)
                   ) as avg_days
                   FROM memory_logs
                   WHERE valid_until IS NOT NULL AND valid_from IS NOT NULL"""
            ).fetchone()
            avg_days = round(avg_lifespan[0], 1) if avg_lifespan[0] else None

            # temporal 엣지 상태
            active_edges = conn.execute(
                "SELECT COUNT(*) FROM rule_edges WHERE valid_until IS NULL"
            ).fetchone()[0]
            expired_edges = conn.execute(
                "SELECT COUNT(*) FROM rule_edges WHERE valid_until IS NOT NULL"
            ).fetchone()[0]

        return {
            "total_rules": total,
            "active_rules": active,
            "superseded_rules": superseded,
            "versioned_rules": versioned,
            "total_versions": total_versions,
            "avg_lifespan_days": avg_days,
            "active_edges": active_edges,
            "expired_edges": expired_edges,
        }


# ═══════════════════════════════════════════════════════════
# Enhanced Preflight — 4가지 진화 통합
# ═══════════════════════════════════════════════════════════

class EnhancedPreflight:
    """Rule Graph + Predictive TGL + Adaptive Trigger + Temporal Graph를 통합한 강화 preflight.

    기존 preflight 결과에 추가로:
    1. 캐스케이드 규칙 (그래프 이웃)
    2. 예측적 TGL 경고
    3. 미매칭 기록 (향후 트리거 확장에 사용)
    4. Temporal 필터링 (대체된 규칙 제외)
    """

    def __init__(self, db: MemoryDB):
        self.db = db
        self.graph = RuleGraph(db=self.db)
        self.predictor = PredictiveTGL(db=self.db)
        self.retriever = HybridRetriever(db=self.db)
        self.temporal = TemporalGraph(db=self.db)

    def enhanced_preflight(self, query: str, limit: int = 5) -> dict:
        """강화된 preflight 실행.

        Returns:
        {
            "tcl_hits": [...],          # 직접 매칭된 TCL (활성만)
            "tgl_hits": [...],          # 직접 매칭된 TGL (활성만)
            "cascade_hits": [...],      # 그래프 캐스케이드 규칙
            "predictions": [...],       # 예측적 TGL 경고
            "general_hits": [...],      # 기타
        }
        """
        # 1. 기존 하이브리드 검색
        base = self.retriever.preflight(query, limit=limit)

        # 2. Temporal 필터: 대체된(superseded) 규칙 제외
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for cat in ("tcl_hits", "tgl_hits", "general_hits"):
            base[cat] = [
                hit for hit in base.get(cat, [])
                if not hit.get("valid_until") or hit["valid_until"] > now
            ]

        # 3. 직접 매칭된 규칙 ID 수집
        direct_ids = []
        for cat in ("tcl_hits", "tgl_hits", "general_hits"):
            for hit in base.get(cat, []):
                if isinstance(hit.get("id"), int):
                    direct_ids.append(hit["id"])

        # 4. Rule Graph 캐스케이드
        cascade = []
        if direct_ids:
            cascade = self.graph.get_cascade_rules(direct_ids, threshold=0.3)
            # 캐스케이드에서도 superseded 제외
            cascade = [
                c for c in cascade
                if not c.get("valid_until") or c["valid_until"] > now
            ]
            # co-activation 기록
            self.graph.record_co_activation(query, direct_ids)

        # 5. Predictive TGL — 매칭된 TGL에 대한 후속 에러 예측
        predictions = []
        for tgl in base.get("tgl_hits", []):
            if isinstance(tgl.get("id"), int):
                preds = self.predictor.predict_next_errors(tgl["id"], min_confidence=0.3)
                for p in preds:
                    predictions.append({
                        "triggered_by": tgl["id"],
                        "predicted_error": p["correction_rule"],
                        "confidence": p["confidence"],
                        "context_tags": p["context_tags"],
                    })

        return {
            "tcl_hits": base.get("tcl_hits", []),
            "tgl_hits": base.get("tgl_hits", []),
            "cascade_hits": cascade,
            "predictions": predictions,
            "general_hits": base.get("general_hits", []),
        }


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("TEMS — Topological Evolving Memory System")
        print()
        print("Usage:")
        print("  python tems_engine.py search <query>        # 하이브리드 검색")
        print("  python tems_engine.py epreflight <query>    # 강화 preflight (그래프+예측 포함)")
        print("  python tems_engine.py health                # 전체 건강 리포트")
        print("  python tems_engine.py sweep                 # 생명주기 스윕")
        print("  python tems_engine.py system                # 시스템 건강도")
        print("  python tems_engine.py sync                  # QMD 동기화")
        print("  python tems_engine.py exception <desc>      # 예외 분류")
        print("  python tems_engine.py graph-build           # 규칙 그래프 구축")
        print("  python tems_engine.py graph-stats           # 그래프 통계")
        print("  python tems_engine.py patterns              # TGL 시퀀스 패턴")
        print("  python tems_engine.py miss-stats             # 미매칭 통계")
        print("  python tems_engine.py expand-triggers       # 트리거 자동 확장")
        print("  python tems_engine.py temporal-stats        # 시간축 통계")
        print("  python tems_engine.py timeline <rule_id>    # 규칙 시간축 이력")
        print("  python tems_engine.py at-time <timestamp>   # 특정 시점의 활성 규칙")
        print("  python tems_engine.py supersede <old> <new> # 규칙 대체")
        sys.exit(0)

    cmd = sys.argv[1]
    db = MemoryDB()

    if cmd == "search" and len(sys.argv) >= 3:
        query = " ".join(sys.argv[2:])
        retriever = HybridRetriever(db)
        results = retriever.search(query)
        for r in results:
            print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            print("---")

    elif cmd == "health":
        scorer = HealthScorer(db)
        report = scorer.get_health_report()
        for r in report:
            print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            print("---")

    elif cmd == "sweep":
        scorer = HealthScorer(db)
        transitions = scorer.run_lifecycle_sweep()
        print("Lifecycle sweep results:", json.dumps(transitions))

        certifier = AnomalyCertifier(db)
        exc_results = certifier.run_exception_sweep()
        print("Exception sweep results:", json.dumps(exc_results))

    elif cmd == "system":
        meta = MetaRuleEngine(db)
        health = meta.compute_system_health()
        print("System Health:", json.dumps(health, indent=2))
        suggestion = meta.suggest_weight_adjustment()
        if suggestion:
            print("\nWeight Adjustment Suggestion:")
            print(json.dumps(suggestion, indent=2, default=str))

    elif cmd == "sync":
        count = sync_rules_to_qmd(db)
        print(f"Synced {count} rules to QMD")

    elif cmd == "exception" and len(sys.argv) >= 3:
        desc = " ".join(sys.argv[2:])
        certifier = AnomalyCertifier(db)
        result = certifier.classify_exception(desc)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "epreflight" and len(sys.argv) >= 3:
        query = " ".join(sys.argv[2:])
        ep = EnhancedPreflight(db)
        result = ep.enhanced_preflight(query)
        for label, hits in result.items():
            if hits:
                print(f"\n=== {label} ===")
                for h in hits:
                    if isinstance(h, dict):
                        rule = h.get("correction_rule", h.get("predicted_error", ""))
                        print(f"  → {str(rule)[:80]}")

    elif cmd == "graph-build":
        graph = RuleGraph(db)
        edges = graph.build_keyword_edges()
        print(f"Keyword overlap edges created: {edges}")
        print(json.dumps(graph.get_graph_stats(), indent=2))

    elif cmd == "graph-stats":
        graph = RuleGraph(db)
        print(json.dumps(graph.get_graph_stats(), indent=2))

    elif cmd == "patterns":
        predictor = PredictiveTGL(db)
        patterns = predictor.get_all_patterns(min_occurrences=1)
        if patterns:
            for p in patterns:
                print(f"  {p['pred_tags'][:30]} → {p['succ_tags'][:30]} "
                      f"(count={p['occurrence_count']}, conf={p['confidence']:.2f})")
        else:
            print("(학습된 패턴 없음 — TGL 발동 이력이 축적되면 자동 학습됩니다)")

    elif cmd == "miss-stats":
        adaptive = AdaptiveTrigger(db)
        print(json.dumps(adaptive.get_miss_stats(), ensure_ascii=False, indent=2))

    elif cmd == "expand-triggers":
        adaptive = AdaptiveTrigger(db)
        expanded = adaptive.auto_expand_triggers()
        if expanded:
            for e in expanded:
                print(f"  Rule #{e['rule_id']}: +{e['added_keywords']} (total={e['total_trigger_size']})")
        else:
            print("(확장할 트리거 없음)")

    elif cmd == "temporal-stats":
        tg = TemporalGraph(db)
        stats = tg.get_temporal_stats()
        print("Temporal Stats:", json.dumps(stats, indent=2, default=str))

    elif cmd == "timeline" and len(sys.argv) >= 3:
        tg = TemporalGraph(db)
        rule_id = int(sys.argv[2])
        timeline = tg.get_rule_timeline(rule_id)
        print(json.dumps(timeline, ensure_ascii=False, indent=2, default=str))

    elif cmd == "at-time" and len(sys.argv) >= 3:
        tg = TemporalGraph(db)
        ts = " ".join(sys.argv[2:])
        rules = tg.query_at_time(ts)
        print(f"Active rules at {ts}:")
        for r in rules:
            print(f"  [{r['category']}] #{r['id']}: {(r.get('correction_rule') or '')[:60]}")

    elif cmd == "supersede" and len(sys.argv) >= 4:
        tg = TemporalGraph(db)
        old_id, new_id = int(sys.argv[2]), int(sys.argv[3])
        reason = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
        ok = tg.supersede_rule(old_id, new_id, reason)
        print(f"Supersede #{old_id} → #{new_id}: {'OK' if ok else 'FAILED'}")

    else:
        print(f"Unknown command: {cmd}")
