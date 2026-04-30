"""TEMS - Topological Evolving Memory System.

Self-evolving agent memory with FTS5+BM25, topological health scoring,
and hybrid sparse-dense retrieval.
"""

__version__ = "0.4.0"

from .fts5_memory import MemoryDB
from .tems_engine import (
    HybridRetriever,
    HealthScorer,
    AnomalyCertifier,
    MetaRuleEngine,
    RuleGraph,
    PredictiveTGL,
    AdaptiveTrigger,
    TemporalGraph,
    EnhancedPreflight,
    sync_rules_to_qmd,
    sync_single_rule_to_qmd,
)
from .rebuild_from_qmd import parse_qmd_rule, rebuild
from .scaffold import get_registry_path

__all__ = [
    "MemoryDB",
    "HybridRetriever",
    "HealthScorer",
    "AnomalyCertifier",
    "MetaRuleEngine",
    "RuleGraph",
    "PredictiveTGL",
    "AdaptiveTrigger",
    "TemporalGraph",
    "EnhancedPreflight",
    "sync_rules_to_qmd",
    "sync_single_rule_to_qmd",
    "parse_qmd_rule",
    "rebuild",
    "get_registry_path",
]
