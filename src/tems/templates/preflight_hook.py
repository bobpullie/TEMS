"""
TEMS Preflight Hook — UserPromptSubmit 자동 트리거 (범용 템플릿)
================================================================
에이전트의 .claude/tems_agent_id 마커 파일을 기반으로 DB를 찾고,
preflight 검색을 수행합니다.
"""

import sys
import json
import re
from pathlib import Path

from tems.fts5_memory import MemoryDB
from tems.tems_engine import EnhancedPreflight, RuleGraph, HybridRetriever


def find_agent_root(start: Path) -> Path:
    """상위 순회하며 .claude/tems_agent_id 찾기 (.git 탐색과 동일 패턴)"""
    cur = start.resolve()
    while cur != cur.parent:
        marker = cur / ".claude" / "tems_agent_id"
        if marker.exists():
            return cur
        cur = cur.parent
    raise FileNotFoundError("tems_agent_id not found from " + str(start))


# 에이전트 자기 식별
AGENT_ROOT = find_agent_root(Path(__file__).parent)
AGENT_ID = (AGENT_ROOT / ".claude" / "tems_agent_id").read_text(encoding="utf-8").strip()
DB_PATH = AGENT_ROOT / "memory" / "error_logs.db"
import os
_reg_env = os.environ.get("TEMS_REGISTRY_PATH")
REGISTRY_PATH = Path(_reg_env) if _reg_env else None


def strip_korean_suffix(word: str) -> str:
    """한국어 단어에서 조사/어미를 제거하여 어간을 추출.

    완벽한 형태소 분석은 아니지만, FTS5 prefix 매칭과 결합하여
    '퇴근할게요' → '퇴근', '마무리합시다' → '마무리' 등을 처리합니다.
    """
    # 흔한 어미/조사 패턴 (긴 것부터 매칭)
    suffixes = [
        # 종결어미
        "할게요", "합시다", "합니다", "했습니다", "하겠습니다",
        "할까요", "해주세요", "해볼게요", "해봅시다",
        "입니다", "습니다", "됩니다", "겠습니다",
        "할게", "할까", "하자", "해요", "해줘", "하죠",
        "인데요", "인데", "이에요", "이야",
        # 연결어미
        "하면서", "하면", "하고", "해서", "하니까", "하지만",
        "인데", "이라", "이면", "이고",
        # 관형형
        "하는", "했던", "할",
        # 조사
        "에서는", "에서", "에는", "에게", "까지", "부터",
        "으로", "에도", "이나", "이란",
        "에", "를", "을", "는", "은", "이", "가", "의", "와", "과",
        "도", "만", "로",
        # 보조용언
        "했어요", "했어", "했다", "해야",
        "됐어요", "됐어", "됐다",
        "시키", "하기", "되기",
        # 기타 활용형
        "할게요", "할까요", "합시다",
        "했는데", "하는데", "되는데",
        "거든요", "거든",
        "잖아요", "잖아",
        "네요", "군요",
    ]

    for suffix in suffixes:
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            return word[: -len(suffix)]

    return word


def extract_keywords(prompt: str, max_tokens: int = 20) -> list[str]:
    """프롬프트에서 BM25 검색용 키워드를 추출.

    불용어를 제거하고, 한국어 어미를 정리한 뒤, 의미 있는 토큰만 남깁니다.
    반환값은 리스트 — 각 키워드로 개별 OR 검색을 수행합니다.
    """
    # 한국어/영어 불용어
    stopwords = {
        "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과",
        "도", "만", "에서", "까지", "부터", "으로", "하고", "그리고",
        "또는", "및", "등", "것", "수", "때", "중", "후", "더",
        "좀", "잘", "한", "할", "해", "된", "되", "하는", "합니다",
        "해주세요", "부탁", "감사", "네", "예", "아니", "오늘", "내일",
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "shall",
        "i", "you", "he", "she", "it", "we", "they",
        "my", "your", "his", "her", "its", "our", "their",
        "this", "that", "these", "those",
        "in", "on", "at", "to", "for", "of", "with", "by",
        "from", "up", "about", "into", "through", "during",
        "and", "but", "or", "not", "no", "so", "if", "then",
        "please", "thanks", "yes", "no",
    }

    tokens = []
    for word in prompt.split():
        cleaned = word.strip(".,!?;:\"'()[]{}~`@#$%^&*+=<>/\\|")
        if not cleaned or len(cleaned) <= 1:
            continue

        # 한국어 어미 제거
        stem = strip_korean_suffix(cleaned)

        if stem.lower() not in stopwords and len(stem) > 1:
            tokens.append(stem)

    # 중복 제거
    seen = set()
    unique = []
    for t in tokens:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)

    return unique[:max_tokens]


## Context Budget — 주입 상한 (관리군 v2026.3.29 도입)
MAX_TCL = 2      # TCL 최대 주입 수
MAX_TGL = 2      # TGL 최대 주입 수
MAX_CASCADE = 1  # CASCADE 최대 주입 수
MAX_PREDICT = 1  # 예측 최대 주입 수
BM25_WEIGHT = 0.6
THS_WEIGHT = 0.4


def get_ths_scores() -> dict[int, tuple[float, str]]:
    """rule_health 테이블에서 (rule_id → (ths_score, status)) 매핑 로드"""
    import sqlite3
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT rule_id, ths_score, status FROM rule_health").fetchall()
        conn.close()
        return {r["rule_id"]: (r["ths_score"] or 0.5, r["status"] or "warm") for r in rows}
    except Exception:
        return {}


def rank_by_ths(hits: list[dict], ths_map: dict[int, tuple[float, str]]) -> list[dict]:
    """BM25 순위(리스트 순서)와 THS 점수를 결합하여 재정렬.

    archive 상태 규칙은 제외.
    """
    scored = []
    for rank, hit in enumerate(hits):
        rid = hit.get("id")
        ths_score, status = ths_map.get(rid, (0.5, "warm"))

        # archive 상태 규칙은 주입에서 제외
        if status == "archive":
            continue

        # BM25 순위 점수: 1위=1.0, 이후 감소
        bm25_score = 1.0 / (1 + rank)
        final_score = BM25_WEIGHT * bm25_score + THS_WEIGHT * ths_score
        hit["_final_score"] = final_score
        hit["_ths"] = ths_score
        hit["_status"] = status
        scored.append(hit)

    scored.sort(key=lambda x: x["_final_score"], reverse=True)
    return scored


def format_rules(preflight_result: dict, compact: bool = True) -> str:
    """preflight 결과를 위상군 컨텍스트 주입용 텍스트로 포맷.

    compact=True: summary만 출력 (컨텍스트 절약)
    compact=False: correction_rule 전문 출력 (기존 방식)

    v2026.3.29: THS 가중치 적용 + 컨텍스트 버짓 도입 (관리군)
    """
    # THS 점수 로드
    ths_map = get_ths_scores()

    # THS 기반 재정렬 + archive 제외
    tcl_hits = rank_by_ths(preflight_result.get("tcl_hits", []), ths_map)[:MAX_TCL]
    tgl_hits = rank_by_ths(preflight_result.get("tgl_hits", []), ths_map)[:MAX_TGL]
    cascade_hits = rank_by_ths(preflight_result.get("cascade_hits", []), ths_map)[:MAX_CASCADE]
    predictions = preflight_result.get("predictions", [])[:MAX_PREDICT]

    if not tcl_hits and not tgl_hits and not cascade_hits and not predictions:
        return ""

    lines = []
    lines.append("<preflight-memory-check>")

    if tcl_hits:
        lines.append("[TCL]")
        for r in tcl_hits:
            text = r.get("summary") or r.get("correction_rule", "") if compact else r.get("correction_rule", "")
            lines.append(f"  #{r.get('id', '?')}: {text}")

    if tgl_hits:
        lines.append("[TGL]")
        for r in tgl_hits:
            text = r.get("summary") or r.get("correction_rule", "") if compact else r.get("correction_rule", "")
            lines.append(f"  #{r.get('id', '?')}: {text}")

    if cascade_hits:
        lines.append("[CASCADE]")
        for r in cascade_hits:
            text = r.get("summary") or r.get("correction_rule", "") if compact else r.get("correction_rule", "")
            lines.append(f"  #{r.get('id', '?')}: [{r.get('category', '?')}] {text}")

    if predictions:
        lines.append("[PREDICT]")
        for p in predictions:
            conf = p.get("confidence", 0)
            lines.append(f"  ({conf:.0%}) {p.get('predicted_error', '')[:40]}")

    lines.append("</preflight-memory-check>")
    return "\n".join(lines)


def detect_project_scope(agent_id: str) -> list[str]:
    """tems_registry.json에서 에이전트의 프로젝트 조회"""
    scopes = ["project:meta", "project:all", ""]
    try:
        if REGISTRY_PATH is None:
            return scopes
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        projects = registry.get("agents", {}).get(agent_id, {}).get("projects", [])
        for p in projects:
            scopes.append(f"project:{p.lower()}")
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # 레지스트리 없으면 기본 스코프만 사용
    return scopes


def filter_by_project(hits: list[dict], allowed_scopes: list[str]) -> list[dict]:
    """규칙의 context_tags에서 project: 태그를 확인하고 스코프 밖 규칙을 제거."""
    filtered = []
    for hit in hits:
        tags = str(hit.get("context_tags", ""))

        # 스킬로 전환된 규칙은 preflight에서 제외
        rule = str(hit.get("correction_rule", ""))
        if rule.startswith("/") and "스킬로" in rule:
            continue

        # project 태그 추출
        project_tag = ""
        for part in tags.split(","):
            part = part.strip()
            if part.startswith("project:"):
                project_tag = part
                break

        # 허용된 스코프에 포함되면 통과
        if project_tag in allowed_scopes:
            filtered.append(hit)

    return filtered


## 규칙성 패턴 감지 — TEMS 자동 등록 유도 (1차 방어선)
TCL_PATTERNS = [
    r"이제부터\s", r"앞으로\s", r"항상\s", r"매번\s", r"반드시\s",
    r"from\s+now\s+on", r"always\s", r"every\s+time", r"규칙으로\s", r"원칙으로\s",
]
TGL_PATTERNS = [
    r"하지\s*마", r"금지", r"절대\s", r"하면\s*안", r"never\s",
    r"don'?t\s", r"do\s+not\s", r"prohibited", r"사용하지\s", r"쓰지\s*마",
]


def detect_rule_intent(prompt: str) -> str | None:
    """사용자 프롬프트에서 규칙성 의도를 감지.

    Returns: "TCL", "TGL", or None
    """
    for pat in TGL_PATTERNS:
        if re.search(pat, prompt, re.IGNORECASE):
            return "TGL"
    for pat in TCL_PATTERNS:
        if re.search(pat, prompt, re.IGNORECASE):
            return "TCL"
    return None


def main():
    try:
        # stdin에서 hook 데이터 읽기
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)

        data = json.loads(raw)
        prompt = data.get("prompt", "")
        cwd = data.get("cwd", "")

        if not prompt.strip():
            sys.exit(0)

        # 프로젝트 스코프 감지 (v2026.3.29 관리군)
        allowed_scopes = detect_project_scope(AGENT_ID)

        # 키워드 추출
        keywords = extract_keywords(prompt)
        if not keywords:
            sys.exit(0)

        db = MemoryDB(db_path=str(DB_PATH))

        # FTS5 prefix 쿼리 구성
        fts_query = " OR ".join(f'"{kw}"*' for kw in keywords)

        # 1단계: FTS5 BM25 기본 검색
        try:
            base_result = db.preflight(fts_query, limit=5)
        except Exception:
            base_result = {"tcl_hits": [], "tgl_hits": [], "general_hits": []}
            seen_ids = set()
            for kw in keywords[:5]:
                try:
                    partial = db.preflight(f'"{kw}"*', limit=3)
                    for cat in ("tcl_hits", "tgl_hits", "general_hits"):
                        for hit in partial.get(cat, []):
                            if hit["id"] not in seen_ids:
                                seen_ids.add(hit["id"])
                                base_result[cat].append(hit)
                except Exception:
                    continue

        # 1-b단계: BM25가 빈약하면 HybridRetriever 시맨틱 폴백
        total_bm25 = sum(len(base_result.get(c, [])) for c in ("tcl_hits", "tgl_hits"))
        if total_bm25 < 2:
            try:
                hybrid = HybridRetriever(db=db, collection=f"tems-{AGENT_ID}")
                hybrid_result = hybrid.preflight(" ".join(keywords), limit=5)
                # BM25 결과에 dense 결과 병합 (중복 제거)
                existing_ids = set()
                for cat in ("tcl_hits", "tgl_hits", "general_hits"):
                    for hit in base_result.get(cat, []):
                        existing_ids.add(hit.get("id"))

                for cat in ("tcl_hits", "tgl_hits", "general_hits"):
                    for hit in hybrid_result.get(cat, []):
                        if hit.get("id") not in existing_ids:
                            base_result[cat].append(hit)
                            existing_ids.add(hit.get("id"))
            except Exception:
                pass

        # 2단계: Rule Graph 캐스케이드 — 직접 매칭된 규칙의 이웃도 포함
        direct_ids = []
        for cat in ("tcl_hits", "tgl_hits", "general_hits"):
            for hit in base_result.get(cat, []):
                if isinstance(hit.get("id"), int):
                    direct_ids.append(hit["id"])

        cascade_hits = []
        predictions = []

        if direct_ids:
            try:
                graph = RuleGraph(db)
                cascade_hits = graph.get_cascade_rules(direct_ids, threshold=0.3)
                # co-activation 기록 (그래프 학습)
                graph.record_co_activation(prompt, direct_ids)
            except Exception:
                pass

            # 3단계: Predictive TGL — TGL이 매칭되었으면 후속 에러 예측
            try:
                from tems.tems_engine import PredictiveTGL
                predictor = PredictiveTGL(db)
                for tgl in base_result.get("tgl_hits", []):
                    if isinstance(tgl.get("id"), int):
                        preds = predictor.predict_next_errors(tgl["id"], min_confidence=0.3)
                        for p in preds:
                            predictions.append({
                                "predicted_error": p.get("correction_rule", ""),
                                "confidence": p.get("confidence", 0),
                            })
            except Exception:
                pass

        # 프로젝트 스코핑 필터 적용 (v2026.3.29 관리군)
        filtered_tcl = filter_by_project(base_result.get("tcl_hits", []), allowed_scopes)
        filtered_tgl = filter_by_project(base_result.get("tgl_hits", []), allowed_scopes)
        filtered_cascade = filter_by_project(cascade_hits, allowed_scopes)
        filtered_general = filter_by_project(base_result.get("general_hits", []), allowed_scopes)

        # 통합 결과
        result = {
            "tcl_hits": filtered_tcl,
            "tgl_hits": filtered_tgl,
            "cascade_hits": filtered_cascade,
            "predictions": predictions,
            "general_hits": filtered_general,
        }

        # 규칙성 패턴 감지 — TEMS 등록 유도 힌트 주입
        rule_type = detect_rule_intent(prompt)
        if rule_type:
            print(f"<rule-detected type=\"{rule_type}\">")
            print(f"종일군의 지시에 규칙성 패턴이 감지되었습니다. AutoMemory가 아닌 TEMS에 등록하세요:")
            print(f'python "{AGENT_ROOT}/memory/tems_commit.py" --type {rule_type} --rule "규칙 내용" --triggers "키워드" --tags "태그"')
            print(f"</rule-detected>")

        # 매칭 결과 포맷
        output = format_rules(result)
        if output:
            print(output)

    except Exception:
        # hook 실패 시 조용히 종료 (위상군 동작을 방해하지 않음)
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
