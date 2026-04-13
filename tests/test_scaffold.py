"""Scaffold + registry tests."""

import json
import os
import pytest
from pathlib import Path
from tems.scaffold import (
    create_marker,
    create_directories,
    create_database,
    get_registry_path,
    load_registry,
    save_registry,
    update_registry,
)


class TestGetRegistryPath:
    def test_env_var_override(self, tmp_path, monkeypatch):
        reg_path = tmp_path / "custom_registry.json"
        monkeypatch.setenv("TEMS_REGISTRY_PATH", str(reg_path))
        result = get_registry_path()
        assert result == reg_path

    def test_default_fallback_missing(self, monkeypatch):
        monkeypatch.delenv("TEMS_REGISTRY_PATH", raising=False)
        result = get_registry_path()
        assert result is None or isinstance(result, Path)


class TestCreateMarker:
    def test_create_new(self, tmp_path):
        result = create_marker(tmp_path, "testagent", force=False)
        assert result == "marker_created"
        marker = tmp_path / ".claude" / "tems_agent_id"
        assert marker.read_text(encoding="utf-8").strip() == "testagent"

    def test_existing_same_id(self, tmp_path):
        create_marker(tmp_path, "testagent", force=False)
        result = create_marker(tmp_path, "testagent", force=False)
        assert result == "marker_exists"

    def test_existing_different_id_raises(self, tmp_path):
        create_marker(tmp_path, "agent_a", force=False)
        with pytest.raises(ValueError, match="different ID"):
            create_marker(tmp_path, "agent_b", force=False)

    def test_force_overwrite(self, tmp_path):
        create_marker(tmp_path, "agent_a", force=False)
        result = create_marker(tmp_path, "agent_b", force=True)
        assert result == "marker_created"


class TestCreateDirectories:
    def test_creates_memory_and_qmd(self, tmp_path):
        actions = create_directories(tmp_path)
        assert "memory_dir_created" in actions
        assert "qmd_rules_dir_created" in actions
        assert (tmp_path / "memory").is_dir()
        assert (tmp_path / "memory" / "qmd_rules").is_dir()


class TestCreateDatabase:
    def test_creates_db_with_full_schema(self, tmp_path):
        (tmp_path / "memory").mkdir()
        result = create_database(tmp_path, force=False)
        assert result == "db_created"

        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "memory" / "error_logs.db"))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()

        for expected in ["memory_logs", "rule_health", "exceptions",
                         "meta_rules", "rule_edges", "co_activations"]:
            assert expected in tables


class TestRegistryCRUD:
    def test_load_save_roundtrip(self, tmp_path):
        reg_path = tmp_path / "registry.json"
        registry = load_registry(reg_path)
        assert registry["version"] == 1
        registry["agents"]["test"] = {"name": "Test", "status": "active"}
        save_registry(registry, reg_path)

        loaded = load_registry(reg_path)
        assert "test" in loaded["agents"]

    def test_update_registry_new_agent(self, tmp_path):
        reg_path = tmp_path / "registry.json"
        result = update_registry(
            "testagent", "Test Agent", "TestProject",
            str(tmp_path / "memory" / "error_logs.db"),
            registry_path=reg_path,
        )
        assert result == "agent_registered"

        registry = load_registry(reg_path)
        assert "testagent" in registry["agents"]
        assert "TestProject" in registry["projects"]
