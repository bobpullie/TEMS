# TEMS — Topological Evolving Memory System

**Upstream:** https://github.com/bobpullie/TEMS

Self-evolving agent memory system with FTS5+BM25 retrieval, topological health scoring, and hybrid sparse-dense search.

## Install

```bash
# From git (recommended)
pip install git+https://github.com/bobpullie/TEMS.git

# Editable (development)
git clone https://github.com/bobpullie/TEMS.git
cd TEMS && pip install -e ".[dev]"
```

## Updating

```bash
pip install -U git+https://github.com/bobpullie/TEMS.git
# 후 에이전트별 재스캐폴딩 (템플릿 변경분 반영)
tems scaffold --agent-id <AGENT_ID> --agent-name "<NAME>" --project <PROJECT> --cwd <PATH>
```

위상군/타 에이전트가 upstream에 기여 → `pip install -U` 로 업데이트 수신. 각 에이전트 로컬 `memory/*.py` 재스캐폴딩이 필요할 수 있음 (self-contained Tier1 특성).

## CLI

```bash
tems scaffold --agent-id myagent --agent-name "My Agent" --project MyProject --cwd /path/to/agent
tems init-skill
```
