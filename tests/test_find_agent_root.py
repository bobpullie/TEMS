"""find_agent_root 다단 해상 — env > cwd walk > __file__ walk > raise.

위상군 인계 (2026-04-30): canonical site-packages install 시 ``start`` 가
``.../site-packages/tems/templates`` 라 marker 못 찾고 무조건 fail.
PR #19 의 _resolve_qmd_cmd 와 대칭 패턴으로 다단 해상 도입.
"""
from pathlib import Path

import pytest

from tems.templates.preflight_hook import find_agent_root


def _make_agent(tmp_path: Path, name: str = "agent") -> Path:
    """Scaffold a minimal agent root with the marker file."""
    root = tmp_path / name
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "tems_agent_id").write_text("test-agent", encoding="utf-8")
    return root


def test_find_agent_root_env_var_precedence(tmp_path, monkeypatch):
    """TEMS_AGENT_ROOT env var must be returned even when cwd or __file__
    would also resolve to a different agent — env wins outright."""
    env_root = _make_agent(tmp_path, "env_agent")
    other_root = _make_agent(tmp_path, "other_agent")

    monkeypatch.setenv("TEMS_AGENT_ROOT", str(env_root))
    monkeypatch.chdir(other_root)  # cwd points to a different valid agent

    # start path inside other_root would normally win the __file__ walk too
    start = other_root / "memory"
    start.mkdir()

    assert find_agent_root(start) == env_root


def test_find_agent_root_cwd_walk(tmp_path, monkeypatch):
    """No env var: cwd walk discovers the marker before __file__ walk does."""
    monkeypatch.delenv("TEMS_AGENT_ROOT", raising=False)
    cwd_root = _make_agent(tmp_path, "cwd_agent")
    sub = cwd_root / "deep" / "nested"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    # start is set to a totally unrelated dir — only cwd walk can succeed
    start = tmp_path / "outside"
    start.mkdir()
    assert find_agent_root(start) == cwd_root


def test_find_agent_root_file_walk_fallback(tmp_path, monkeypatch):
    """cwd walk fails (cwd outside any agent) → __file__ walk succeeds.

    This is the v0.4 editable-in-project install scenario that must remain
    backward-compatible.
    """
    monkeypatch.delenv("TEMS_AGENT_ROOT", raising=False)

    # cwd outside any agent
    cwd_only = tmp_path / "no_marker_here"
    cwd_only.mkdir()
    monkeypatch.chdir(cwd_only)

    # __file__ start lives inside a real agent
    file_root = _make_agent(tmp_path, "file_agent")
    start = file_root / "src" / "tems" / "templates"
    start.mkdir(parents=True)

    assert find_agent_root(start) == file_root


def test_find_agent_root_env_invalid_raises(tmp_path, monkeypatch):
    """TEMS_AGENT_ROOT 가 박혔는데 marker 없으면 silent fallback 하지 말고 즉시
    raise — wrong-agent-root migration 의 silent drift 차단."""
    bogus = tmp_path / "bogus_root"
    bogus.mkdir()  # no .claude/tems_agent_id

    monkeypatch.setenv("TEMS_AGENT_ROOT", str(bogus))

    # Even if cwd + start are valid agents, env-invalid must raise.
    valid_root = _make_agent(tmp_path, "valid")
    monkeypatch.chdir(valid_root)
    start = valid_root / "src"
    start.mkdir()

    with pytest.raises(FileNotFoundError, match="TEMS_AGENT_ROOT"):
        find_agent_root(start)


def test_find_agent_root_site_packages_no_env(tmp_path, monkeypatch):
    """canonical site-packages install 시뮬레이션 — env 부재 + cwd 가 marker 밖
    + start 가 marker 밖 → 명확한 진단 raise."""
    monkeypatch.delenv("TEMS_AGENT_ROOT", raising=False)

    cwd_only = tmp_path / "nowhere"
    cwd_only.mkdir()
    monkeypatch.chdir(cwd_only)

    site_packages = tmp_path / "site_packages" / "tems" / "templates"
    site_packages.mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="tems_agent_id not found"):
        find_agent_root(site_packages)


def test_module_import_graceful_outside_agent_tree(tmp_path, monkeypatch):
    """위상군 인계의 본질 시나리오 — pip install + ``from tems.templates import
    preflight_hook`` 는 module load 자체가 fail 하면 안 됨. AGENT_ROOT 가
    None 으로 끝나도 import 는 통과해야 함."""
    import importlib
    from tems.templates import preflight_hook

    monkeypatch.delenv("TEMS_AGENT_ROOT", raising=False)
    nowhere = tmp_path / "no_marker_here"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)

    # Reload the module under the simulated site-packages-like environment.
    # This re-runs all module-level code including AGENT_ROOT resolution.
    importlib.reload(preflight_hook)

    # Import succeeded (no FileNotFoundError raised) and AGENT_ROOT is None —
    # signalling main() must lazy-resolve at hook execution time.
    assert preflight_hook.AGENT_ROOT is None
    assert preflight_hook.AGENT_ID is None
    assert preflight_hook.DB_PATH is None
