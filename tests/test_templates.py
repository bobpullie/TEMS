"""Verify templates are accessible as package_data."""

import importlib.resources
from pathlib import Path


def test_templates_exist():
    """All 3 template files must be accessible via importlib.resources."""
    for name in ("preflight_hook.py", "tems_commit.py", "gitignore.template"):
        ref = importlib.resources.files("tems") / "templates" / name
        path = Path(str(ref))
        assert path.exists(), f"Template missing: {name}"


def test_preflight_uses_tems_import():
    """New template must import from 'tems' not 'tems_core'."""
    ref = importlib.resources.files("tems") / "templates" / "preflight_hook.py"
    content = Path(str(ref)).read_text(encoding="utf-8")
    assert "from tems." in content or "import tems" in content
    assert "tems_core" not in content
    assert "E:/AgentInterface" not in content


def test_tems_commit_uses_tems_import():
    """New template must import from 'tems' not 'tems_core'."""
    ref = importlib.resources.files("tems") / "templates" / "tems_commit.py"
    content = Path(str(ref)).read_text(encoding="utf-8")
    assert "from tems." in content or "import tems" in content
    assert "tems_core" not in content
    assert "E:/AgentInterface" not in content
