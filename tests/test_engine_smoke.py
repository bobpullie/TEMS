"""Smoke test: tems_engine imports correctly and core classes are accessible."""


def test_engine_imports():
    from tems.tems_engine import HybridRetriever, HealthScorer, RuleGraph
    assert HybridRetriever is not None
    assert HealthScorer is not None
    assert RuleGraph is not None


def test_rebuild_imports():
    from tems.rebuild_from_qmd import parse_qmd_rule, rebuild
    assert parse_qmd_rule is not None
    assert rebuild is not None
