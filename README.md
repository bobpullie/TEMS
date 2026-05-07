# TEMS — Topological Evolving Memory System

> ClaudeCode에이전트와 대규모 프로젝트 진행시 갈수록 쌓여가는 규칙과 프로토콜들로 무거워지는 하네스를 프로젝트 맥락에 맞춰 한정된 컨텍스트메모리 안에서 효율적으로 자동운영. LLM 에이전트의 행동을 규칙으로 구조화하여 반복 실수를 자동 차단·교정하는 자기진화 메모리 시스템. 한글기반 대규모 vibe코딩프로젝트 특화.

**Upstream:** https://github.com/bobpullie/TEMS

---

## 5-Asset 4 원칙 (Independence + Local-Only + Separate-Repo + Universal Portability)

본 자산은 Triad Chord Studio 5-Asset 체계 (**TEMS / SDC / DVC / TWK / handover**) 의 한 축. 다음 4 게이트 원칙을 모두 만족해야 canonical GitHub 레포에 push 허용:

1. **Independence** — 5자산 상호 의존 0. 본 자산은 다른 4 자산이 미설치돼도 self-contained 작동.
2. **Local-Only** — 모든 에이전트 자산 (룰 / DB / QMD / 핸드오버 / 위키) 은 에이전트 로컬 프로젝트 폴더 내부에만. hub 디렉토리 (예: `E:/AgentInterface/`) 금지. (TEMS Python 패키지 자체는 site-packages 에 들어가지만 에이전트 룰/DB 등 자산은 모두 프로젝트 로컬.)
3. **Separate-Repo** — 각 자산 별도 canonical 레포 보유. 한 PR 에 두 레포 묶지 않음.
4. **Universal Portability** — Windows/Linux/macOS, 임의 OS user, 임의 에이전트명 (한국어/영어 무관) 작동. 절대경로/특정 user-name/hub 의존 금지 — env var · marker walk · `Path(__file__)` · `importlib.resources` 만 허용.

위반 발견 시 즉시 일반화 PR. 미충족 = canonical push 보류.

---

## TL;DR

TEMS 는 다음 세 가지를 한꺼번에 해결하는 경량 SQLite 기반 프레임워크다.

1. **행동 규칙을 영속화** — 사용자 지시와 누적 실수를 구조화된 규칙(TCL/TGL)으로 DB 에 저장
2. **매 대화마다 관련 규칙을 자동 주입** — `UserPromptSubmit` hook 에서 BM25 매칭으로 현재 맥락에 맞는 규칙만 LLM 컨텍스트에 삽입
3. **준수/위반을 자동 측정하고 진화** — `PostToolUse` 에서 위반을 감지해 카운팅, 반복 위반 규칙은 hook 레벨로 승격 또는 archive

CLAUDE.md / system prompt 에 자연어 지시를 쌓아올리는 방식과 다르게, TEMS 는 **규칙을 데이터로 취급**하여 검색·카운팅·decay 가 가능하다.

---

## Install

```bash
# From git (recommended)
pip install git+https://github.com/bobpullie/TEMS.git

# Editable (development)
git clone https://github.com/bobpullie/TEMS.git
cd TEMS && pip install -e ".[dev]"
```

## Quickstart

신규 에이전트 환경 부트스트랩:

```bash
tems scaffold \
  --agent-id myagent \
  --agent-name "My Agent" \
  --project MyProject \
  --cwd /path/to/agent
```

이 명령은 다음을 자동 수행한다.
- `.claude/tems_agent_id` 마커 생성
- `memory/` + `memory/qmd_rules/` 디렉토리 생성
- `memory/error_logs.db` SQLite 스키마 초기화 (Phase 2A/3 컬럼 포함)
- `memory/*.py` 템플릿 10종 복사 (preflight, tool_gate, compliance_tracker 등)
- `.gitignore` 에 TEMS runtime state 항목 추가
- `.claude/settings.local.json` 에 hook 6종 등록 (UserPromptSubmit / PreToolUse / PostToolUse / Stop)

## Updating

```bash
pip install -U git+https://github.com/bobpullie/TEMS.git
# 후 에이전트별 재스캐폴딩 (템플릿 변경분 반영, DB 데이터 보존)
tems scaffold --agent-id <AGENT_ID> --agent-name "<NAME>" --project <PROJECT> --cwd <PATH>
# 또는 restore (레지스트리 기반, 누락 템플릿만 복사)
tems restore --agent-id <AGENT_ID>
```

업그레이드 시 `rule_health` 테이블에 누락된 Phase 2A/3 컬럼(fire_count / compliance_count / violation_count / classification 등)이 자동 ALTER TABLE 로 보충된다 (기존 데이터 보존).

---

## Hybrid Search — Dense + BM25 (v0.3+ 기본 활성)

TEMS v0.3부터 한글 의미 검색을 기본 지원한다. BM25 키워드 매칭만으로는 어휘 변형·활용형·동의어에 취약하다는 한계를 Dense 벡터 검색으로 보강한다.

**작동 방식:**
- LM Studio, Ollama, vLLM 등 OpenAI-compat `/v1/embeddings` 엔드포인트를 자동 감지
- 한글 e2e 임베딩 latency 측정 기반 자동 판별: **평균 < 300ms → dense main(가중치 0.8), BM25 보강(0.2)**
- CUDA 없는 Vulkan iGPU 환경(미니PC, AMD 노트북)도 지원 — Qwen3-Embedding-0.6B-Q8_0 + Vulkan 기준 평균 31ms 달성
- 측정 실패(서버 미기동 / latency ≥ 300ms) 시 BM25-only로 자동 폴백

**환경변수:**

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `TEMS_EMBED_URL` | `http://localhost:1234/v1` | LM Studio 또는 기타 임베딩 서버 주소 |
| `TEMS_EMBED_MODEL` | 자동 감지 (첫 임베딩 모델) | 사용할 임베딩 모델 ID |
| `TEMS_DENSE` | 미설정 (자동) | `0` = 강제 BM25-only, `1` = 강제 dense 활성 |

```bash
# LM Studio 기본값 사용 시 (포트 1234, 임베딩 모델 자동 감지)
tems scaffold ...  # 자동 감지 후 dense 활성

# 특정 모델 지정
TEMS_EMBED_MODEL=Qwen3-Embedding-0.6B-Q8_0 python memory/preflight_hook.py

# BM25 only 강제 (CI, 서버 등 임베딩 서버 없는 환경)
TEMS_DENSE=0 python memory/tems_commit.py --type TCL ...
```

**구현 모듈:**
- `dense_backend.py` — `OpenAICompatBackend` + `detect_backend()` 자동 감지 (외부 deps 0, urllib.request만 사용)
- `vector_store.py` — SQLite BLOB 저장 + 코사인 전체스캔 (1,000 규칙 < 100ms)

**한국어 keyword_trigger 자동 보완 (v0.2.2~ 유지):** `commit_memory()` 호출 시 `keyword_trigger` 내 한국어 단어의 어간이 자동 추가됨 (예: `"퇴근합시다"` → `"퇴근합시다 퇴근"`). FTS5 BM25 prefix 매칭 적중률 향상용. v0.3에서도 동일하게 동작 — Dense fallback 시 sparse 보강 효과.

---

## Korean Agent Use Cases — TEMS의 가치

한글 환경 에이전트에서 Dense + BM25 하이브리드가 BM25-only 대비 실질적으로 달라지는 3가지 사례.

### 1. 대규모 코드 빌드 (장시간 세션)

```
# 시나리오: 50파일 변경, 컴파일 에러 23개, 30분 디버깅 세션

[BM25 only]
사용자: "import 순환 참조 같은데"
→ "import" 키워드 매칭 → 일반 import 가이드 5개 → 노이즈

[Dense + BM25 (v0.3)]
사용자: "import 순환 참조 같은데"
→ 의미 임베딩 → "Python 패키징 — flat layout pythonpath" 규칙 #1
→ "circular import에서 부분 모듈만 가져오기" 사례 #2
→ 정확히 30분 전에 본인이 등록한 같은 패턴 회상
```

### 2. 한글 맥락 명령

```
[BM25 only]
사용자: "퇴근하자"
→ keyword_trigger 매칭 실패 (FTS5 한글 토크나이저 한계)
→ 세션 종료 hook 미발동

[Dense + BM25 (v0.3)]
사용자: "퇴근하자"
→ 의미 임베딩 → "session shutdown trigger" 의미 매칭
→ handover_doc 자동 생성 hook 발동
```

### 3. 한영 혼용 코드베이스

```
[BM25 only]
사용자: "이거 deprecate 처리해줘"
→ "deprecate" 영문 토큰만 매칭 → 영문 가이드만 회수

[Dense + BM25 (v0.3)]
사용자: "이거 deprecate 처리해줘"
→ 한국어 "처리" + 영어 "deprecate" 의미 결합
→ "API deprecation 한글 changelog 작성 규칙" 회수
```

---

## Auto-Setup — 벡터 환경 자동 안내 (PR2 예정)

- 첫 실행 시 임베딩 서버 미감지 → Vulkan/CUDA 검사 → 설치 가이드 자동 제시
- 한글 locale 감지 시 Qwen3-Embedding-0.6B 권장
- 영문 위주 시 embeddinggemma-300M 권장

---

## 왜 필요한가 ? 

CLAUDE.md 같은 정적 지시 파일의 한계:

| 문제 | 정적 지시 | TEMS |
|------|----------|------|
| 컨텍스트 희석 | 모든 지시가 매 turn 주입 → 길어지면 무시됨 | 관련 규칙만 강매칭 시 주입 |
| 효용도 측정 불가 | 어느 지시가 실효인지 알 수 없음 | `fire_count` / `compliance_count` / `violation_count` 자동 누적 |
| 누적 실수 학습 불가 | 같은 실수를 세션마다 반복 | `pattern_detector` 로 N≥5회 반복 패턴 자동 TGL 등록 |
| 강제력 없음 | LLM 순응에만 의존 | TGL-T 는 `PreToolUse` 에서 도구 호출 자체를 harness 차단 |
| 검색 불가 | 키워드로 찾을 수 없음 | FTS5 BM25 검색 |

---

## 핵심 개념

### TCL vs TGL

규칙은 일반화 수준에 따라 두 타입으로 분리된다. 혼용 등록 금지.

| 축 | **TCL** (Topological Checklist Loop) | **TGL** (Topological Guard Loop) |
|----|--------------------------------------|----------------------------------|
| 본질 | 명시적 사용자 지시 · 국지적 규약 | 누적 실수에서 추출한 카테고리 가드 |
| 트리거 | "앞으로/이제부터/항상/반드시" 명시 | 동일 시그니처 N≥5회 자동 감지 또는 명시 지시 |
| 일반화 | L1 Concrete Pattern | L2 Topological Case (sweet spot 강제) |
| 매칭 | BM25 키워드(1차) | dense semantic(1차) + BM25(보강) |
| 예시 | "세션 종료 시 핸드오버 작성" | "외부 패키지는 `pip show` 로 존재 검증 후 import" |

**L2 Topological Case** 가 sweet spot — L0 는 1회성이라 재사용 불가, L1 은 여전히 구체적, L3 이상은 도메인 초월이라 과잉 적용. L2 는 "여러 도메인에 걸쳐 같은 구조의 실수가 재발하는 경우" 를 잡는 추상화 수준.

### TGL 7-카테고리

TGL 은 **발동 Hook 시점** 기준으로 7개 카테고리로 분류된다.

| 코드 | 본질 | 발동 시점 | 핵심 질문 |
|------|------|-----------|----------|
| **TGL-T** | Tool Action — 도구 호출 자체가 위험 | PreToolUse | 이 도구를 호출하면 안 되는가? |
| **TGL-S** | System State — 사전조건 깨짐 | SessionStart | 시스템이 전제조건을 만족하는가? |
| **TGL-D** | Dependency — 외부 의존성 부재/변경 | runtime exception | 이 패키지가 실제 존재하는가? |
| **TGL-P** | Pattern Reuse — 코드 패턴 반복 버그 | 코드 작성 | 이 패턴을 전에 잘못 쓴 적 있는가? |
| **TGL-W** | Workflow — 작업 흐름/순서 위반 | 단계 전환 | 이 단계를 건너뛰면 무슨 오류가? |
| **TGL-C** | Communication — 정보 전달 결함 | 위임/보고 | 올바른 에이전트에게 전달되는가? |
| **TGL-M** | Meta-system — TEMS/hook 자체 변경 | 시스템 변경 | TEMS 자체를 건드리는가? |

---

## 아키텍처 개요

```
사용자 프롬프트
      │
      ▼
┌─────────────────────┐
│ UserPromptSubmit    │  preflight_hook.py
│ ─ BM25 검색         │  → <preflight-memory-check>
│ ─ score gate        │    TGL/TCL 규칙 주입
│                     │    + violation_count 노출 (Layer 1)
└──────────┬──────────┘
           │
           ▼
    LLM (Claude)
           │
           ▼
┌─────────────────────┐
│ PreToolUse          │  tool_gate_hook.py
│ ─ TGL-T tool_pattern│  → severity=critical 매칭 시
│   regex 매칭        │    deny JSON 반환 (Layer 2, hard block)
│ ─ self-invocation   │    → severity=warning 은 경고만
│   제외              │
└──────────┬──────────┘
           │ (deny 아니면 통과)
           ▼
     도구 실행
           │
           ▼
┌─────────────────────┐
│ PostToolUse         │  compliance_tracker.py
│ ─ forbidden/        │  → window 내 위반 없으면 compliance++
│   failure_signature │  → 위반 감지 시 violation++ (Layer 3)
│   매칭              │  → window 만료 후 guard 제거
│ ─ TTL 만료 guard    │
│   청소              │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Stop (session end)  │  retrospective_hook.py
│ ─ 세션 교훈 추출    │
└─────────────────────┘
```

### 패키지 구조 (`bobpullie/TEMS`)

```
src/tems/
├── __init__.py
├── cli.py                     `tems` 명령 진입점
├── scaffold.py                신규 에이전트 부트스트랩 / restore / registry 관리
├── fts5_memory.py             MemoryDB (BM25 전문검색)
├── tems_engine.py             HybridRetriever / RuleGraph / PredictiveTGL / HealthScorer
├── rebuild_from_qmd.py        qmd_rules → DB 재구축
├── skill/
│   └── SKILL.md               Claude Code Skill 정의 (/tems)
└── templates/                 에이전트별 `memory/` 로 복사되는 hook 스크립트
    ├── preflight_hook.py           UserPromptSubmit — 규칙 주입 (Layer 1)
    ├── tems_commit.py              규칙 등록 CLI
    ├── tool_gate_hook.py           PreToolUse — TGL-T deny (Layer 2)
    ├── compliance_tracker.py       PostToolUse — 위반/준수 측정 (Layer 3)
    ├── tool_failure_hook.py        PostToolUse Bash — 실패 시그니처 탐지
    ├── retrospective_hook.py       Stop — 세션 종료 교훈 추출
    ├── pattern_detector.py         반복 패턴 자동 TGL 등록
    ├── memory_bridge.py            PostToolUse Write|Edit — 파일 변경 학습
    ├── decay.py                    cold 전환 / archive (cron)
    ├── sdc_commit.py               서브에이전트 위임 계약 CLI
    └── gitignore.template          `.gitignore` 항목 템플릿
```

### 에이전트별 스캐폴딩 후 구조

```
<agent_root>/
├── .claude/
│   ├── tems_agent_id          마커 (scaffold 시 생성)
│   └── settings.local.json    hook 등록 (6종 이벤트)
├── memory/
│   ├── error_logs.db          SQLite 본체 (git tracked 금지)
│   ├── qmd_rules/             규칙 정규 소스 (tems_commit.py 자동 생성)
│   ├── active_guards.json     현재 활성 guard (compliance window 추적)
│   ├── compliance_events.jsonl 위반/준수 이벤트 로그
│   ├── tems_diagnostics.jsonl hook 실패 진단 로그
│   ├── sdc_briefs.jsonl       SDC 계약 제출 로그
│   └── *.py                   scaffold 가 복사한 hook 스크립트 10종
└── .gitignore                 TEMS runtime state 섹션 포함
```

### DB 스키마 (핵심 테이블)

| 테이블 | 용도 |
|--------|------|
| `memory_logs` | 규칙 본문 (category, correction_rule, context_tags, severity, summary) |
| `memory_fts` | FTS5 전문검색 가상 테이블 |
| `rule_health` | `ths_score`, `fire_count`, `compliance_count`, `violation_count`, `status` (hot/warm/cold/archive), `classification`, `needs_review` |
| `exceptions` | 예외 케이스 (승격 이력, persistence_score) |
| `meta_rules` | 메타 규칙 조절 이력 (가중치 변경 근거) |
| `rule_edges` | 규칙 간 그래프 엣지 (co-activation) |
| `co_activations` | 동시 발동 기록 (그래프 학습) |
| `tgl_sequences` | 규칙 predecessor/successor 관계 (predictive TGL) |
| `trigger_misses` | 매칭 실패 쿼리 로그 |
| `rule_versions` | 규칙 수정 히스토리 |

---

## 런타임 라이프사이클 (실제 예시)

### Step 1 — 프롬프트 도착

```
사용자: "useEffect deps 에 currentPrice 넣어서 interval 재생성 문제 있는데"
```

### Step 2 — preflight 키워드 추출 + BM25 검색

```python
keywords = ["useEffect", "deps", "currentPrice", "interval", "재생성"]
fts_query = '"useEffect"* OR "deps"* OR "currentPrice"* OR ...'
→ TGL #54 매칭 (final_score=0.73, THRESHOLD=0.7)
```

### Step 3 — 컨텍스트 주입

```
<preflight-memory-check>
[TGL] 필수 준수 — 위반 시 rule_health.violation_count 자동 증가
  #54 (v:0 c:11): useEffect deps 에 매 틱 변하는 값(currentPrice 등)을
    넣으면 interval/timer 가 매번 재생성되어 모션 재생이 끊긴다.
    useRef 로 최신값을 래핑하고 deps 에서 제외하여 stale closure 를 방지할 것.
→ 위 TGL 중 해당되는 규칙을 응답에 명시하세요.
  예: "TGL #54 에 따라 X 대신 Y 사용". 주입된 TGL: #54
</preflight-memory-check>
```

`(v:0 c:11)` = 이 규칙은 지금까지 위반 0회, 준수 11회 — LLM 은 "잘 지켜지는 규칙" 으로 인지. 반대로 `(v:14 c:1)` 이면 반복 위반 경고 신호.

### Step 4 — LLM 응답

```
TGL #54 에 따라 currentPrice 를 useRef 로 래핑하고 deps 에서 제외...
```

### Step 5 — 도구 호출 (Edit)

`PreToolUse` → `tool_gate_hook` 가 TGL-T tool_pattern 검사. 매칭 없으면 통과.

### Step 6 — 도구 완료 후

`PostToolUse` → `compliance_tracker` 가 `active_guards.json` 의 #54 guard 를 검사. `forbidden`/`failure_signature` 미매칭, window 만료 시 `compliance_count++`.

---

## 강제력 계층 (Enforcement Layers)

TEMS 는 규칙 강제력을 4층으로 분리해 적용한다. 강할수록 구현·유지보수 비용이 크므로 필요에 따라 승격한다.

| Layer | 수단 | 강제력 | 구현 파일 | 적용 대상 |
|-------|------|--------|-----------|----------|
| **L1** | 자연어 주입 강화 + violation_count 노출 | 소프트 (LLM 순응 의존) | `preflight_hook.py` `format_rules` | 모든 TGL |
| **L2** | PreToolUse `permissionDecision: "deny"` JSON | 하드 (harness 가 도구 호출 차단) | `tool_gate_hook.py` | TGL-T `tool_pattern` + `severity=critical` |
| **L3** | PostToolUse compliance 측정 + violation_count 누적 | 사후 적발 + 데이터화 | `compliance_tracker.py` | 모든 TGL (`forbidden`/`failure_signature` 슬롯 보유) |
| **L4** | DVC case 승격 (결정론적 빌드 검증) | CI/cron 차단 | 별도 DVC 시스템 | 빌드 산출물 기반 체크 가능한 규칙 |

**설계 원칙:**
- 자연어 주입은 근본적으로 소프트 — 컨텍스트 길어지면 희석됨.
- 정규식/패턴 매칭 가능한 TGL-T → L2 하드 차단 승격.
- 사후 적발로 충분한 규칙 → L3 (violation_count 누적으로 반복 위반 자동 식별).

**Layer 2 deny JSON 스펙:**
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "TGL #N (TGL-T) — 도구 호출 차단\n패턴: ...\n..."
  }
}
```
이 JSON 을 hook 이 stdout 으로 출력하면 Claude Code harness 가 도구 호출 자체를 취소한다 (stdout 텍스트 경고보다 훨씬 강함).

---

## 조용한 TEMS (Quiet TEMS) 정책

매 prompt 무차별 주입은 "banner blindness" 를 일으켜 모든 규칙이 무시된다. TEMS 는 다음 임계값 gate 를 적용한다 (실제 운영 에이전트 값 참고):

```python
SCORE_THRESHOLD = 0.7    # final_score < 이 값이면 주입 안 함
MAX_TCL = 2              # TCL 최대 주입 수
MAX_TGL = 2              # TGL 최대 주입 수

final_score = 0.6 * BM25_rank_score + 0.4 * THS_score
```

- `BM25_rank_score = 1 / (1 + rank)` — 키워드 매칭 순위
- `THS_score` — 규칙 효용도 (0~1, `rule_health.ths_score`)

결과: 매 turn 평균 2~4개 규칙만 주입, 무관한 규칙은 침묵.

### THS_score 활성화 (필수 운영 절차)

`compliance_tracker` 가 `rule_health.ths_score` 를 `INSERT` 시 0.5 로 초기화 후
갱신하지 않는다 — 그대로 두면 모든 룰이 default 0.5 에 묶여 `THS_WEIGHT(0.4)` 가
차별화 신호를 잃고 BM25 단일 신호가 ranking 을 결정한다. 결과: generic-keyword
noise 룰이 specific-keyword 적중 룰을 이긴다.

따라서 주기적으로 ths_score 를 재계산해야 한다 (decay 와 함께 cron 권장):

```bash
python memory/decay.py --recompute-ths              # 적용
python memory/decay.py --recompute-ths --dry-run    # 시뮬레이션
```

공식: `ths = 0.5 + (utility - 0.5) * confidence`
- `utility = compliance / (compliance + violation)` (신호 없으면 0.5)
- `confidence = min(1.0, fire_count / 10)`

자주 발화 + 잘 지켜지는 룰 → ths ↑. 자주 발화 + 위반 우세 → ths ↓ (룰 본문 점검 신호).

---

## 규칙 진화 (Self-Evolution)

### Trigger Counting

preflight 주입 시 `fire_count++` 자동 갱신. 매칭만이 아니라 **실제 주입된** 규칙만 카운트.

### Health States

| 상태 | 조건 | 효과 |
|------|------|------|
| `hot` | 최근 빈번히 발동 | 주입 우선순위 상승 |
| `warm` | 정상 | 기본 |
| `cold` | 30일 0회 발동 | 자동 전환, 주입 감점 |
| `archive` | 90일 0회 | 주입 대상에서 제외 |

`python memory/decay.py` 를 cron 으로 돌려 상태 전환 (기본 미활성).

### Pattern Detection (자동 TGL 등록)

`pattern_detector.py` 는 `compliance_events.jsonl` 과 실패 로그를 스캔하여 N≥5회 반복된 동일 시그니처를 자동 TGL 로 등록. `needs_review=1` 태그 부여 → 나중에 수동 재분류 필요.

활성화 조건: `TEMS 자동등록 활성화` TCL 이 등록되어 있어야 함.

---

## 규칙 등록 (Rule Registration)

### TCL 등록

## CLI

```bash
python memory/tems_commit.py --type TCL \
  --rule "세션 종료 트리거(퇴근|종료|마무리) 감지 시 핸드오버 문서 작성" \
  --triggers "퇴근,종료,마무리,끝,핸드오버,session-end" \
  --tags "project:myproject,domain:lifecycle"
```

- `--triggers` 는 BM25 매칭을 위한 동의어 (5개 이상 권장)
- 게이트 A (스키마) + 키워드 다양성 검사 통과 시 DB 적재

### TGL 등록

```bash
python memory/tems_commit.py --type TGL \
  --classification TGL-D \
  --topological_case "외부 패키지 import 전 실제 설치 여부 미검증 → ModuleNotFoundError" \
  --forbidden "pip show 없이 import 시도" \
  --required "pip show {package} + python -c 'import {package}' 선행 확인" \
  --failure_signatures "ModuleNotFoundError,ImportError: No module named" \
  --tags "project:all,domain:dependency"
```

- `--classification` 필수 (TGL-T/S/D/P/W/C/M 중 1)
- 카테고리별 추가 슬롯 (`tool_patterns` for TGL-T 등) 필수
- 게이트 A + B (거부형) + C/D/E (경고형) 통과 시 적재

### 게이트 요약

| 게이트 | 대상 | 내용 | 형태 |
|--------|------|------|------|
| A | TCL/TGL | 스키마 완성도 | 거부 |
| B | TGL | L0/L4 추상화 수준 거부 | 거부 |
| C | TGL | 중복 탐지 | 경고 |
| D | TGL | 재현성 검증 | 경고 |
| E | TGL | 검증 가능성 | 경고 |
| 키워드 다양성 | TCL | triggers 80% 이상 커버 | 거부 |

---

## Registry (선택적 — 여러 에이전트 관리)

환경변수 `TEMS_REGISTRY_PATH` 로 공용 레지스트리 파일을 지정하면 여러 에이전트를 중앙 관리할 수 있다.

```bash
export TEMS_REGISTRY_PATH=/path/to/tems_registry.json
tems scaffold ...                        # 자동으로 레지스트리 등록
tems add --agent-id X --project Y        # 기존 에이전트에 프로젝트 추가
tems rename --old OldName --new NewName  # 프로젝트 이름 변경 (전 에이전트 갱신)
tems retire --agent-id X                 # 에이전트 은퇴
tems reactivate --agent-id X             # 재활성
tems restore --agent-id X                # 인프라 복구 (데이터 보존)
```

---

## 자기 관찰 (Diagnostics)

실패는 조용히 먹지 않는다:

- hook 전반 예외: `memory/tems_diagnostics.jsonl` 구조화 로그
- preflight 실패 시: `<preflight-degraded reason="..."/>` 컨텍스트 주입 (silent fail 금지)
- 모든 hook 은 **절대 blocking 되지 않음** — 진단 로그 남기고 exit 0

### 건강 확인 쿼리

```bash
# 가장 자주 위반되는 규칙
sqlite3 memory/error_logs.db "
  SELECT m.id, rh.violation_count, substr(m.correction_rule, 1, 60)
  FROM memory_logs m JOIN rule_health rh ON rh.rule_id = m.id
  ORDER BY rh.violation_count DESC LIMIT 10
"

# 발동 없는 cold 후보
sqlite3 memory/error_logs.db "
  SELECT m.id, m.category, rh.last_fired
  FROM memory_logs m JOIN rule_health rh ON rh.rule_id = m.id
  WHERE rh.status = 'cold' OR rh.fire_count = 0
"

# 최근 위반/준수 이벤트
tail -20 memory/compliance_events.jsonl | jq .
```

---

## 버전 / Phase 이력

| 버전 | Phase | 내용 |
|------|-------|------|
| 0.1.0 | Phase 2 | self-contained retrieval + 게이트 A~E + 패키지화 + scaffold CLI |
| 0.2.0 | Phase 3 + Layer 1 강화 | tool_gate_hook (deny), compliance_tracker, decay, pattern_detector, tool_failure, retrospective, memory_bridge, sdc_commit 템플릿 추가. preflight 에 violation_count 노출 + 필수 준수 헤더. scaffold 가 6개 hook 이벤트 등록. Phase 2→3 in-place DB 마이그레이션. |
| 0.2.1 | Patch | Template preflight 의 `detect_project_scope` 가 Registry 미설정 시 cwd fallback 으로 `project:X` 태그 규칙 매칭 가능. `__version__` 상수 동기화. QMD Dense Fallback README 섹션 추가. |
| 0.3.0 | Dense Backend | QMD CLI 제거 → LM Studio `/v1/embeddings` 직호출 (`dense_backend.py`, `vector_store.py`). dense 가용성 판별 기준을 nvidia-smi → 한글 e2e latency < 300ms로 변경. Vulkan iGPU 환경 지원. `TEMS_EMBED_URL` / `TEMS_EMBED_MODEL` 환경변수 도입. 가중치 반전: dense 0.8 main, BM25 0.2 보강. `tems embed [--force]` CLI 명령 추가. |
| 0.3.1 | Cleansed init | History squash — 사용자명/회사명/한국어 에이전트명/절대경로 익명화 + LICENSE MIT 정정. 단일 init commit. |
| **0.4.0** | **THS 회귀 정정 + Diagnostics & Self-Audit + direct run 호환** | **3건 PR 누적 (PR #4 / #5 / #6).**<br>**(1) `compute_ths` input swap (PR #4)** — dead column (`activation_count` / `correction_success/total` / `last_activated`) → alive source (`fire_count` / `compliance/violation` 비율 / `last_fired` fallback chain). 기존 모든 rule THS = 0.5 default 회귀 정정. `compute_system_health` last_fired migration + ISO 4-format fallback. `MemoryDB.supersede_rule` 에 `record_modification` fail-soft wire. `.resolve()` canonical 9 templates 일괄 (cwd 비의존).<br>**(2) Diagnostics & Self-Audit layer (PR #5)** — 신규 3 templates: `run_decay_if_due.py` (SessionStart 24h 가드, cron 대체) / `audit_diagnostics_recent.py` (24h `*_failure` 가시화, α layer) / `audit_dead_state.py` (정적 dead-state 검출 dev tool). `scaffold._PHASE4_TEMPLATES` 신설 + `_HOOK_PLAN` 4-tuple `(event, matcher, script, args)` 지원 + `register_hook` args 처리. SessionStart 신규 2 entries (run_decay + audit_diag `--silent --hours 24`).<br>**(3) Direct run 호환 (PR #6)** — `tems_engine.py` 의 top-level relative import (`from .fts5_memory`) → absolute (`from tems.fts5_memory`) + sys.path 보강. `python src/tems/tems_engine.py` 직접 실행 시 ImportError 차단. 정상 사용 경로 (`python -m tems.tems_engine`, pytest, import-as-module) 영향 0.<br>**Schema migration 불필요** — 0.3.1 init schema 가 이미 신규 컬럼 보유. 산식 weight (ALPHA~EPSILON) 보존, input source 만 swap.** |

---

## 관련 시스템과의 분리

| 시스템 | 목적 | 식별자 | 위치 | 계층 |
|--------|------|--------|------|------|
| **TEMS** | LLM 행동 교정 | `#N` 정수 | `memory/error_logs.db` | 런타임 (매 prompt) |
| **DVC** | 결정론적 빌드 검증 | `DOMAIN_VERB_NNN` | 별도 checklist 시스템 | CI/cron (빌드 시) |
| **SDC** | 서브에이전트 위임 계약 | Q1/Q2/Q3 gate | `memory/sdc_briefs.jsonl` | 세션 내 결정 |

---

## Contributing

이슈 / PR 환영.

- Upstream: https://github.com/bobpullie/TEMS
- TEMS 는 에이전트를 이용한 나의 여러 프로젝트자동화와 독립적인 앱들을 제작하면서 경험적으로 도출한 구조
- 외부 레퍼런스 없이 자체 개발 (empirical architecture, no paper citations)

## License

MIT (see [LICENSE](LICENSE))
