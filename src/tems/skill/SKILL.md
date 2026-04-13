---
name: tems
description: TEMS (Topological Evolving Memory System) - agent self-evolving memory. Use when committing rules (TCL/TGL), debugging preflight, or checking rule health.
---

# TEMS - Topological Evolving Memory System

## Overview
4-Phase self-evolving memory:
1. **Hybrid Retrieval** - FTS5 BM25 (sparse) + QMD vector (dense) + RRF fusion
2. **Health Scoring** - THS lifecycle (hot -> warm -> cold -> archive)
3. **Anomaly Certification** - Exception classification + rule promotion
4. **Meta-Rule Engine** - Godel Agent self-modification

## Rule Types
- **TCL** (Topological Checklist Loop): proactive checklist
- **TGL** (Topological Guard Loop): defensive guard rule

## Rule Registration
```bash
python memory/tems_commit.py --type TCL --rule "rule content" --triggers "keywords" --tags "tags"
python memory/tems_commit.py --type TGL --rule "rule content" --triggers "keywords" --tags "tags"
```

## Preflight
Automatically triggered via UserPromptSubmit hook. Injects `<preflight-memory-check>` with relevant TCL/TGL rules.

## DB Schema (10 tables)
- `memory_logs` - core rule storage
- `rule_health` - THS score + lifecycle status
- `exceptions` - anomaly classification
- `meta_rules` - self-modification audit
- `rule_edges` - topological connections
- `co_activations` - co-firing patterns
- `tgl_sequences` - temporal predecessor chains
- `trigger_misses` - keyword expansion learning
- `rule_versions` - evolution history
- `memory_fts` - FTS5 virtual table

## Troubleshooting
- **DB corruption:** `python -m tems.rebuild_from_qmd --agent-root <path>`
- **Missing rules:** Check `memory/qmd_rules/` (source of truth for rebuild)
- **Preflight silent:** Check `.claude/settings.local.json` hook registration
