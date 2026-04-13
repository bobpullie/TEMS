# TEMS 상세 아키텍처 참조

## 4-Phase 아키텍처
| Phase | 역할 | 엔진 |
|-------|------|------|
| 1 | Hybrid Sparse-Dense Retrieval (FTS5 BM25 + QMD vector + RRF) | `HybridRetriever` |
| 2 | Topological Health Score (THS) — 규칙 생명주기 관리 | `HealthScorer` |
| 3 | Topological Anomaly Certificate (TAC) — 예외 분류 | `AnomalyCertifier` |
| 4 | Meta-Rule Self-Modification — Godel Agent pattern | `MetaRuleEngine` |

## DB 스키마
| 테이블 | 용도 |
|--------|------|
| `memory_logs` | TCL/TGL 규칙 (context_tags, keyword_trigger, correction_rule) |
| `memory_fts` | FTS5 전문검색 가상 테이블 |
| `rule_health` | THS 점수, 활성화/수정 이력, 상태(hot/warm/cold/archive) |
| `exceptions` | 예외케이스 (type A/B/C, persistence_score, 승격 이력) |
| `meta_rules` | 메타규칙 조절 이력 (가중치 변경 근거, 건강도 before/after) |

## 자동 트리거
`UserPromptSubmit` Hook → `preflight_hook.py` → 매칭된 TCL/TGL → `<preflight-memory-check>` 태그로 컨텍스트 주입.
