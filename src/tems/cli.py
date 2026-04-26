"""TEMS CLI — scaffold + init-skill + embed commands."""

import argparse
import importlib.resources
import json
import shutil
import sys
from pathlib import Path

from .scaffold import (
    create_marker,
    create_directories,
    create_database,
    install_gitignore,
    copy_templates,
    register_hook,
    update_registry,
    get_registry_path,
    restore_agent,
    add_project_to_agent,
    rename_project,
    retire_agent,
    reactivate_agent,
)


def cmd_scaffold(args):
    """New agent environment setup."""
    cwd = Path(args.cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    reg_path = Path(args.registry_path) if args.registry_path else get_registry_path()

    actions = []
    try:
        actions.append(create_marker(cwd, args.agent_id, args.force))
        actions.extend(create_directories(cwd))
        actions.append(create_database(cwd, args.force))
        actions.append(install_gitignore(cwd, args.force))
        actions.extend(copy_templates(cwd, args.force))
        actions.append(register_hook(cwd))
        db_path = str(cwd / "memory" / "error_logs.db")
        actions.append(update_registry(
            args.agent_id, args.agent_name, args.project, db_path,
            registry_path=reg_path,
        ))
        result = {"ok": True, "agent_id": args.agent_id, "actions": actions}
    except Exception as e:
        result = {"ok": False, "error": str(e), "actions": actions}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_init_skill(args):
    """Deploy SKILL.md + references to Claude Code skills directory."""
    target = Path(args.target) if args.target else Path.home() / ".claude" / "skills" / "tems"
    target.mkdir(parents=True, exist_ok=True)

    skill_src = importlib.resources.files("tems") / "skill"
    actions = []

    # Copy SKILL.md
    skill_md = Path(str(skill_src / "SKILL.md"))
    if skill_md.exists():
        shutil.copy2(str(skill_md), str(target / "SKILL.md"))
        actions.append("SKILL.md copied")

    # Copy references/
    refs_src = Path(str(skill_src / "references"))
    refs_dst = target / "references"
    refs_dst.mkdir(parents=True, exist_ok=True)
    if refs_src.is_dir():
        for f in refs_src.iterdir():
            shutil.copy2(str(f), str(refs_dst / f.name))
            actions.append(f"references/{f.name} copied")

    result = {"ok": True, "target": str(target), "actions": actions}
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_restore(args):
    """Restore agent from registry."""
    reg_path = Path(args.registry_path) if args.registry_path else None
    try:
        result = restore_agent(args.agent_id, registry_path=reg_path)
    except Exception as e:
        result = {"ok": False, "error": str(e)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_embed(args):
    """인덱스 재생성: embedding_meta와 현재 backend model_id 비교 → 불일치 rule 재임베딩."""
    from .tems_engine import _check_dense_available, get_dense_backend

    if not _check_dense_available():
        result = {"ok": False, "error": "Dense backend not available. Check TEMS_EMBED_URL / LM Studio."}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    backend = get_dense_backend()
    if backend is None:
        result = {"ok": False, "error": "Dense backend cache empty after availability check."}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    # DB 경로 탐색 — TEMS_DB_PATH 환경변수 또는 tems_agent_id marker 순회
    import os
    db_path = os.environ.get("TEMS_DB_PATH", "")
    if not db_path:
        # marker 순회
        cur = Path.cwd()
        while cur != cur.parent:
            marker = cur / ".claude" / "tems_agent_id"
            if marker.exists():
                db_path = str(cur / "memory" / "error_logs.db")
                break
            cur = cur.parent

    if not db_path or not Path(db_path).exists():
        result = {"ok": False, "error": f"DB not found. Set TEMS_DB_PATH or run from agent root."}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    from .fts5_memory import MemoryDB
    from .vector_store import VectorStore

    db = MemoryDB(db_path=db_path)
    store = VectorStore(db_path)
    model_id = backend.model_id

    if args.force:
        # 전체 재임베딩
        with db._conn() as conn:
            rules = conn.execute("SELECT * FROM memory_logs").fetchall()
        target_ids = [r["id"] for r in rules]
    elif args.rule_id is not None:
        target_ids = [args.rule_id]
    else:
        target_ids = store.needs_reindex(model_id)

    if not target_ids:
        result = {"ok": True, "embedded": 0, "message": "All rules already indexed with current model."}
        print(json.dumps(result, ensure_ascii=False))
        return 0

    embedded = 0
    errors = 0

    for rule_id in target_ids:
        try:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT action_taken, correction_rule, keyword_trigger FROM memory_logs WHERE id = ?",
                    (rule_id,),
                ).fetchone()
            if not row:
                continue
            text = f"{row['action_taken'] or ''}\n{row['correction_rule'] or ''}\n{row['keyword_trigger'] or ''}"
            vec = backend.embed(text)
            store.upsert(rule_id, vec, model_id)
            embedded += 1
        except Exception as e:
            errors += 1

    result = {
        "ok": True,
        "embedded": embedded,
        "errors": errors,
        "model_id": model_id,
        "total_targets": len(target_ids),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="tems", description="TEMS - Topological Evolving Memory System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scaffold
    p_scaffold = subparsers.add_parser("scaffold", help="New agent environment setup")
    p_scaffold.add_argument("--agent-id", required=True)
    p_scaffold.add_argument("--agent-name", required=True)
    p_scaffold.add_argument("--project", required=True)
    p_scaffold.add_argument("--cwd", required=True)
    p_scaffold.add_argument("--force", action="store_true")
    p_scaffold.add_argument("--registry-path", default=None)

    # init-skill
    p_init = subparsers.add_parser("init-skill", help="Deploy Claude Code skill")
    p_init.add_argument("--target", default=None)

    # embed (v0.3)
    p_embed = subparsers.add_parser("embed", help="Re-index embeddings for rules")
    p_embed.add_argument("--force", action="store_true", help="Ignore model mismatch, re-embed all rules")
    p_embed.add_argument("--rule-id", type=int, default=None, help="Re-embed a single rule by ID")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore agent from registry")
    p_restore.add_argument("--agent-id", required=True)
    p_restore.add_argument("--registry-path", default=None)

    # add / rename / retire / reactivate (passthrough to scaffold.py)
    p_add = subparsers.add_parser("add", help="Add project to agent")
    p_add.add_argument("--agent-id", required=True)
    p_add.add_argument("--project", required=True)
    p_add.add_argument("--registry-path", default=None)

    p_rename = subparsers.add_parser("rename", help="Rename project")
    p_rename.add_argument("--old", required=True)
    p_rename.add_argument("--new", required=True)
    p_rename.add_argument("--registry-path", default=None)

    p_retire = subparsers.add_parser("retire", help="Retire agent")
    p_retire.add_argument("--agent-id", required=True)
    p_retire.add_argument("--registry-path", default=None)

    p_react = subparsers.add_parser("reactivate", help="Reactivate agent")
    p_react.add_argument("--agent-id", required=True)
    p_react.add_argument("--registry-path", default=None)

    args = parser.parse_args()

    handlers = {
        "scaffold": cmd_scaffold,
        "init-skill": cmd_init_skill,
        "restore": cmd_restore,
        "embed": cmd_embed,
    }

    if args.command in handlers:
        sys.exit(handlers[args.command](args))

    # Simple passthrough commands
    reg_path = Path(args.registry_path) if getattr(args, "registry_path", None) else None
    if args.command == "add":
        result = add_project_to_agent(args.agent_id, args.project, reg_path)
    elif args.command == "rename":
        result = rename_project(args.old, args.new, reg_path)
    elif args.command == "retire":
        result = retire_agent(args.agent_id, reg_path)
    elif args.command == "reactivate":
        result = reactivate_agent(args.agent_id, reg_path)
    else:
        print(json.dumps({"ok": False, "error": f"Unknown command: {args.command}"}))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
