"""
TEMS Dense Backend — LM Studio / OpenAI-compat 임베딩 어댑터
=============================================================
외부 의존성 0: urllib.request + json + time만 사용.
detect_backend()가 Section 2 알고리즘에 따라 자동 감지.
"""

import json
import os
import time
import math
from abc import ABC, abstractmethod
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

# ─── 테스트 문장 (한글 형태소 다양성 확보, spec §2) ─────────────────────
_TEST_SENTENCES = [
    "한글 임베딩 가용성 점검 — 의미 캐리어 검증용 첫번째 문장이다.",
    "TEMS는 위상적 진화 메모리 시스템으로 규칙 자동 회수를 지원한다.",
    "벡터 검색 latency가 임계치 아래면 dense를 우선 사용한다.",
]

_LATENCY_THRESHOLD_MS = 300.0  # ms — spec §2


# ═══════════════════════════════════════════════════════════
# 추상 기반 클래스
# ═══════════════════════════════════════════════════════════

class EmbeddingBackend(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @property
    @abstractmethod
    def model_id(self) -> str: ...


# ═══════════════════════════════════════════════════════════
# OpenAI-compat 어댑터 (LM Studio, Ollama, vLLM 등)
# ═══════════════════════════════════════════════════════════

class OpenAICompatBackend(EmbeddingBackend):
    """OpenAI-compatible /v1/embeddings endpoint 래퍼.

    urllib.request만 사용 — requests/httpx 등 외부 deps 회피.
    """

    def __init__(self, base_url: str, model: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._dim: Optional[int] = None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        if self._dim is None:
            vec = self.embed("dimension probe")
            self._dim = len(vec)
        return self._dim

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._base_url}/embeddings"
        payload = json.dumps({"model": self._model, "input": texts}).encode("utf-8")
        req = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        data = body.get("data", [])
        # /v1/embeddings 응답은 index 순서 보장이 명세상 필수는 아님 → 정렬
        data_sorted = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data_sorted]


# ═══════════════════════════════════════════════════════════
# 내부 유틸 — 코사인 유사도 (numpy 없이)
# ═══════════════════════════════════════════════════════════

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ═══════════════════════════════════════════════════════════
# 자동 감지 (spec §2 의사결정 트리)
# ═══════════════════════════════════════════════════════════

def detect_backend() -> Optional[EmbeddingBackend]:
    """Section 2 알고리즘에 따라 임베딩 백엔드를 자동 감지.

    1. TEMS_EMBED_URL ping
    2. GET /models → 임베딩 모델 검출
    3. 한글 3문장 e2e 측정 — 평균 < 300ms → enable
    4. 실패/타임아웃 → None 반환 (BM25 폴백)
    """
    base_url = os.environ.get("TEMS_EMBED_URL", "http://localhost:1234/v1").rstrip("/")
    forced_model = os.environ.get("TEMS_EMBED_MODEL", "").strip()

    # 1. ping — /models 엔드포인트
    try:
        req = Request(f"{base_url}/models", method="GET")
        with urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, Exception):
        return None

    # 2. 임베딩 모델 검출 (id에 "embed" 포함)
    models_data = body.get("data", [])
    embed_models = [m for m in models_data if "embed" in m.get("id", "").lower()]
    if not embed_models:
        return None

    if forced_model:
        model_id = forced_model
    else:
        model_id = embed_models[0]["id"]

    backend = OpenAICompatBackend(base_url=base_url, model=model_id)

    # 3. 한글 3문장 e2e 측정 (cascade #1: 1회 측정 금지)
    latencies: list[float] = []
    try:
        for sentence in _TEST_SENTENCES:
            t0 = time.perf_counter()
            vec = backend.embed(sentence)
            _ = _cosine(vec, vec)  # e2e에 코사인도 포함
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)
    except Exception:
        return None

    if not latencies:
        return None

    avg_ms = sum(latencies) / len(latencies)
    if avg_ms >= _LATENCY_THRESHOLD_MS:
        return None

    return backend
