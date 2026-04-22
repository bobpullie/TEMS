"""
TEMS Agent Scaffold — 결정론적 에이전트 환경 구축 스크립트

Usage:
  python -m tems.scaffold scaffold --agent-id realgoon --agent-name "리얼군" --project MysticIsland --cwd "E:/00_unrealAgent"
  python -m tems.scaffold scaffold --agent-id realgoon --agent-name "리얼군" --project MysticIsland --cwd "E:/00_unrealAgent" --force
"""

import argparse
import importlib.resources
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

def get_registry_path() -> Path | None:
    """Registry path resolution: TEMS_REGISTRY_PATH env var → None."""
    env = os.environ.get("TEMS_REGISTRY_PATH")
    if env:
        return Path(env)
    return None


def _get_template_path(filename: str) -> Path:
    """Get template file path from package_data via importlib.resources."""
    ref = importlib.resources.files("tems") / "templates" / filename
    return Path(str(ref))


def create_marker(cwd: Path, agent_id: str, force: bool) -> str:
    """Step 1: .claude/tems_agent_id 마커 파일 생성"""
    marker_dir = cwd / ".claude"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / "tems_agent_id"

    if marker.exists() and not force:
        existing = marker.read_text(encoding="utf-8").strip()
        if existing == agent_id:
            return "marker_exists"
        raise ValueError(f"Marker exists with different ID: {existing} (use --force to overwrite)")

    marker.write_text(agent_id, encoding="utf-8")
    return "marker_created"


def create_directories(cwd: Path) -> list[str]:
    """Step 2: memory/ + qmd_rules/ 디렉토리 생성"""
    actions = []
    memory_dir = cwd / "memory"
    qmd_dir = memory_dir / "qmd_rules"

    if not memory_dir.exists():
        memory_dir.mkdir(parents=True)
        actions.append("memory_dir_created")

    if not qmd_dir.exists():
        qmd_dir.mkdir(parents=True)
        actions.append("qmd_rules_dir_created")

    return actions


# Phase 3 rule_health 추가 컬럼 (Phase 2 → Phase 3 마이그레이션 대상).
# 기존 DB 에 누락 시 ALTER TABLE ADD COLUMN 으로 보충.
_RULE_HEALTH_PHASE3_COLUMNS = (
    ("fire_count", "INTEGER DEFAULT 0"),
    ("last_fired", "TEXT"),
    ("compliance_count", "INTEGER DEFAULT 0"),
    ("violation_count", "INTEGER DEFAULT 0"),
    ("created_at", "TEXT"),
    ("classification", "TEXT"),
    ("abstraction_level", "TEXT"),
    ("needs_review", "INTEGER DEFAULT 0"),
)


def _migrate_rule_health(db_path: str) -> list[str]:
    """Phase 2 → Phase 3 in-place 컬럼 마이그레이션. 기존 데이터 보존."""
    conn = sqlite3.connect(db_path)
    try:
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_health)").fetchall()}
    except sqlite3.OperationalError:
        # rule_health 테이블 자체가 없는 경우 — 호출자가 _create_tables 로 처리
        conn.close()
        return []
    added = []
    for col, ddl in _RULE_HEALTH_PHASE3_COLUMNS:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE rule_health ADD COLUMN {col} {ddl}")
            added.append(col)
    if added:
        conn.commit()
    conn.close()
    return added


def create_database(cwd: Path, force: bool) -> str:
    """Step 3: error_logs.db 전체 스키마 생성 (Phase 2 → Phase 3 마이그레이션 포함)."""
    db_path = cwd / "memory" / "error_logs.db"

    if db_path.exists() and not force:
        # 스키마 검증 — 누락 테이블 + Phase 3 rule_health 컬럼 보충
        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        required = {"memory_logs", "rule_health", "exceptions", "meta_rules",
                     "rule_edges", "co_activations", "tgl_sequences", "trigger_misses", "rule_versions"}
        missing = required - set(tables)
        if missing:
            _create_tables(str(db_path), only_missing=missing)
        # Phase 3 컬럼 마이그레이션 — rule_health 가 존재하는 경우
        added_cols = _migrate_rule_health(str(db_path))
        if missing and added_cols:
            return "db_schema_updated_and_migrated"
        if missing:
            return "db_schema_updated"
        if added_cols:
            return "db_phase3_columns_added"
        return "db_exists"

    _create_tables(str(db_path))
    return "db_created"


def _create_tables(db_path: str, only_missing: set = None):
    """전체 TEMS 스키마 생성"""
    conn = sqlite3.connect(db_path)

    schemas = {
        "memory_logs": """
            CREATE TABLE IF NOT EXISTS memory_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                context_tags TEXT NOT NULL,
                keyword_trigger TEXT DEFAULT '',
                action_taken TEXT NOT NULL,
                result TEXT NOT NULL,
                correction_rule TEXT,
                category TEXT DEFAULT 'general',
                severity TEXT DEFAULT 'info',
                summary TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """,
        "rule_health": """
            CREATE TABLE IF NOT EXISTS rule_health (
                rule_id INTEGER PRIMARY KEY,
                activation_count INTEGER DEFAULT 0,
                correction_success INTEGER DEFAULT 0,
                correction_total INTEGER DEFAULT 0,
                modification_count INTEGER DEFAULT 0,
                last_activated TEXT,
                last_modified TEXT,
                status TEXT DEFAULT 'warm',
                status_changed_at TEXT DEFAULT (datetime('now')),
                ths_score REAL DEFAULT 0.5,
                ths_updated_at TEXT DEFAULT (datetime('now')),
                fire_count INTEGER DEFAULT 0,
                last_fired TEXT,
                compliance_count INTEGER DEFAULT 0,
                violation_count INTEGER DEFAULT 0,
                created_at TEXT,
                classification TEXT,
                abstraction_level TEXT,
                needs_review INTEGER DEFAULT 0,
                FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
            )
        """,
        "exceptions": """
            CREATE TABLE IF NOT EXISTS exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,
                exception_type TEXT NOT NULL,
                description TEXT NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                persistence_score REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now')),
                last_seen TEXT DEFAULT (datetime('now')),
                status TEXT DEFAULT 'active',
                promoted_to_rule_id INTEGER,
                FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
            )
        """,
        "meta_rules": """
            CREATE TABLE IF NOT EXISTS meta_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level INTEGER NOT NULL,
                parameter_name TEXT NOT NULL,
                old_value REAL,
                new_value REAL,
                reason TEXT,
                system_health_before REAL,
                system_health_after REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """,
        "rule_edges": """
            CREATE TABLE IF NOT EXISTS rule_edges (
                rule_a INTEGER NOT NULL,
                rule_b INTEGER NOT NULL,
                edge_type TEXT NOT NULL,
                weight REAL DEFAULT 0.0,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (rule_a, rule_b, edge_type)
            )
        """,
        "co_activations": """
            CREATE TABLE IF NOT EXISTS co_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_hash TEXT NOT NULL,
                rule_ids TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """,
        "tgl_sequences": """
            CREATE TABLE IF NOT EXISTS tgl_sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                predecessor_id INTEGER NOT NULL,
                successor_id INTEGER NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                confidence REAL DEFAULT 0.0,
                last_seen TEXT DEFAULT (datetime('now')),
                UNIQUE(predecessor_id, successor_id)
            )
        """,
        "trigger_misses": """
            CREATE TABLE IF NOT EXISTS trigger_misses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                expected_rule_id INTEGER,
                timestamp TEXT DEFAULT (datetime('now'))
            )
        """,
        "rule_versions": """
            CREATE TABLE IF NOT EXISTS rule_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                field_changed TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                timestamp TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (rule_id) REFERENCES memory_logs(id)
            )
        """,
    }

    for name, ddl in schemas.items():
        if only_missing is None or name in only_missing:
            conn.execute(ddl)

    # FTS5 가상 테이블 (memory_logs 기반) — summary 컬럼 포함 (Phase 2A+)
    if only_missing is None or "memory_fts" in only_missing:
        conn.execute("DROP TABLE IF EXISTS memory_fts")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                context_tags, keyword_trigger, action_taken, result,
                correction_rule, category, summary,
                content=memory_logs, content_rowid=id,
                tokenize='unicode61'
            )
        """)
        # 동기화 트리거
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_logs BEGIN
                INSERT INTO memory_fts(rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category, summary)
                VALUES (new.id, new.context_tags, new.keyword_trigger, new.action_taken, new.result, new.correction_rule, new.category, new.summary);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_logs BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category, summary)
                VALUES ('delete', old.id, old.context_tags, old.keyword_trigger, old.action_taken, old.result, old.correction_rule, old.category, old.summary);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_logs BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category, summary)
                VALUES ('delete', old.id, old.context_tags, old.keyword_trigger, old.action_taken, old.result, old.correction_rule, old.category, old.summary);
                INSERT INTO memory_fts(rowid, context_tags, keyword_trigger, action_taken, result, correction_rule, category, summary)
                VALUES (new.id, new.context_tags, new.keyword_trigger, new.action_taken, new.result, new.correction_rule, new.category, new.summary);
            END
        """)

    conn.commit()
    conn.close()


def install_gitignore(cwd: Path, force: bool) -> str:
    """Step: .gitignore에 TEMS 표준 항목 추가 (멱등)"""
    template_path = _get_template_path("gitignore.template")
    if not template_path.exists():
        return "gitignore_template_missing"

    template_content = template_path.read_text(encoding="utf-8")
    gitignore_path = cwd / ".gitignore"

    marker = "# TEMS runtime state"

    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
        if marker in existing:
            return "gitignore_already_has_tems"
        # 추가 모드 (기존 내용 보존)
        new_content = existing.rstrip() + "\n\n" + template_content
        gitignore_path.write_text(new_content, encoding="utf-8")
        return "gitignore_appended"
    else:
        gitignore_path.write_text(template_content, encoding="utf-8")
        return "gitignore_created"


# Phase 2 진입점 템플릿 (기존)
_PHASE2_TEMPLATES = ("preflight_hook.py", "tems_commit.py")

# Phase 3 Enforcement 레이어 — tool_gate_hook (deny), compliance_tracker (측정),
# tool_failure (패턴), retrospective (세션 종료), pattern_detector (자동 등록),
# memory_bridge (파일 변경 학습), decay (건강 진화), sdc_commit (위임 계약 CLI).
_PHASE3_TEMPLATES = (
    "tool_gate_hook.py",
    "compliance_tracker.py",
    "tool_failure_hook.py",
    "retrospective_hook.py",
    "pattern_detector.py",
    "memory_bridge.py",
    "decay.py",
    "sdc_commit.py",
)


def copy_templates(cwd: Path, force: bool) -> list[str]:
    """Step 4: 진입점 템플릿 복사 (Phase 2 + Phase 3 포함)."""
    actions = []
    for filename in _PHASE2_TEMPLATES + _PHASE3_TEMPLATES:
        src = _get_template_path(filename)
        if not src.exists():
            # 템플릿이 패키지에 포함되지 않은 경우 (구버전 호환)
            actions.append(f"{filename}_missing_in_package")
            continue
        dst = cwd / "memory" / filename
        if dst.exists() and not force:
            actions.append(f"{filename}_exists")
            continue
        shutil.copy2(str(src), str(dst))
        actions.append(f"{filename}_copied")
    return actions


# Phase 3 hook event → script 매핑. matcher 는 빈 문자열로 전체 도구 대상.
# - UserPromptSubmit: preflight 규칙 주입 (Phase 2)
# - PreToolUse: TGL-T deny/warning (Phase 3)
# - PostToolUse(ALL): compliance 측정 (Phase 3)
# - PostToolUse(Bash): 실패 시그니처 탐지 (Phase 3)
# - PostToolUse(Write|Edit): 파일 변경 학습 (Phase 2/3)
# - Stop: 세션 종료 교훈 추출 (Phase 3)
_HOOK_PLAN = (
    ("UserPromptSubmit", "", "preflight_hook.py"),
    ("PreToolUse", "", "tool_gate_hook.py"),
    ("PostToolUse", "", "compliance_tracker.py"),
    ("PostToolUse", "Bash", "tool_failure_hook.py"),
    ("PostToolUse", "Write|Edit", "memory_bridge.py"),
    ("Stop", "", "retrospective_hook.py"),
)


def register_hook(cwd: Path) -> list[str]:
    """Step 5: .claude/settings.local.json 에 TEMS hook 등록 (Phase 2 + Phase 3).

    멱등 — 이미 등록된 동일 script 는 경로만 갱신.
    matcher 가 있는 event 는 같은 matcher 안의 동일 script 를 중복 등록하지 않음.
    """
    settings_path = cwd / ".claude" / "settings.local.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        settings = {}
    hooks = settings.setdefault("hooks", {})

    actions: list[str] = []

    for event, matcher, script in _HOOK_PLAN:
        # 해당 event 에 해당 script 가 포함된 템플릿이 실제 복사되었는지 확인
        template_path = _get_template_path(script)
        if not template_path.exists():
            # 패키지에 템플릿이 없는 경우 (구버전 호환) — hook 등록 건너뜀
            actions.append(f"{event}:{script}_skipped_missing_template")
            continue

        new_command = f'python "{cwd}/memory/{script}"'
        event_entries = hooks.setdefault(event, [])

        # matcher 가 같은 entry 를 찾거나 새로 생성
        target_entry = None
        for entry in event_entries:
            if entry.get("matcher", "") == matcher:
                target_entry = entry
                break
        if target_entry is None:
            target_entry = {"matcher": matcher, "hooks": []}
            event_entries.append(target_entry)

        inner_hooks = target_entry.setdefault("hooks", [])

        # 이미 같은 script 가 등록되어 있는지 확인 (basename 매칭)
        existing = None
        for h in inner_hooks:
            if script in h.get("command", ""):
                existing = h
                break

        if existing is not None:
            if existing.get("command") != new_command:
                existing["command"] = new_command
                actions.append(f"{event}:{script}_updated")
            else:
                actions.append(f"{event}:{script}_exists")
        else:
            inner_hooks.append({"type": "command", "command": new_command})
            actions.append(f"{event}:{script}_registered")

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return actions


def load_registry(registry_path: Path = None) -> dict:
    """레지스트리 로드"""
    path = registry_path or get_registry_path()
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "registry_path": str(path or ""), "projects": {}, "agents": {}}


def save_registry(registry: dict, registry_path: Path = None):
    """레지스트리 저장"""
    path = registry_path or get_registry_path()
    if path is None:
        raise ValueError("No registry path available")
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")


def add_project_to_agent(agent_id: str, project: str, registry_path: Path = None) -> dict:
    """기존 에이전트에 프로젝트 추가 소속"""
    registry = load_registry(registry_path)
    now = datetime.now().strftime("%Y-%m-%d")

    if agent_id not in registry["agents"]:
        return {"ok": False, "error": f"Agent '{agent_id}' not found in registry"}

    agent = registry["agents"][agent_id]

    # 프로젝트가 레지스트리에 없으면 자동 생성
    if project not in registry["projects"]:
        registry["projects"][project] = {"aliases": [project], "status": "active"}

    if project in agent.get("projects", []):
        agent["last_verified"] = now
        save_registry(registry, registry_path)
        return {"ok": True, "action": "already_member", "agent_id": agent_id, "project": project}

    agent.setdefault("projects", []).append(project)
    agent["last_verified"] = now
    save_registry(registry, registry_path)
    return {"ok": True, "action": "project_added", "agent_id": agent_id, "project": project}


def rename_project(old_name: str, new_name: str, registry_path: Path = None) -> dict:
    """프로젝트 이름 변경 — 전 에이전트 자동 갱신"""
    registry = load_registry(registry_path)

    if old_name not in registry["projects"]:
        return {"ok": False, "error": f"Project '{old_name}' not found in registry"}

    if new_name in registry["projects"]:
        return {"ok": False, "error": f"Project '{new_name}' already exists in registry"}

    # 프로젝트 엔트리 이동
    project_data = registry["projects"].pop(old_name)
    # aliases 갱신: old_name → new_name
    aliases = project_data.get("aliases", [])
    if old_name in aliases:
        aliases[aliases.index(old_name)] = new_name
    if new_name not in aliases:
        aliases.append(new_name)
    project_data["aliases"] = aliases
    registry["projects"][new_name] = project_data

    # 전 에이전트의 projects 배열 갱신
    affected = []
    for agent_id, agent in registry["agents"].items():
        if old_name in agent.get("projects", []):
            projects = agent["projects"]
            projects[projects.index(old_name)] = new_name
            affected.append(agent_id)

    save_registry(registry, registry_path)
    return {"ok": True, "action": "project_renamed", "old": old_name, "new": new_name, "affected_agents": affected}


def retire_agent(agent_id: str, registry_path: Path = None) -> dict:
    """에이전트 은퇴 — cross-agent 검색에서 제외"""
    registry = load_registry(registry_path)
    now = datetime.now().strftime("%Y-%m-%d")

    if agent_id not in registry["agents"]:
        return {"ok": False, "error": f"Agent '{agent_id}' not found in registry"}

    agent = registry["agents"][agent_id]
    if agent["status"] == "retired":
        return {"ok": True, "action": "already_retired", "agent_id": agent_id}

    agent["status"] = "retired"
    agent["last_verified"] = now
    save_registry(registry, registry_path)
    return {"ok": True, "action": "agent_retired", "agent_id": agent_id}


def reactivate_agent(agent_id: str, registry_path: Path = None) -> dict:
    """은퇴 에이전트 재활성화"""
    registry = load_registry(registry_path)
    now = datetime.now().strftime("%Y-%m-%d")

    if agent_id not in registry["agents"]:
        return {"ok": False, "error": f"Agent '{agent_id}' not found in registry"}

    agent = registry["agents"][agent_id]
    if agent["status"] == "active":
        return {"ok": True, "action": "already_active", "agent_id": agent_id}

    agent["status"] = "active"
    agent["last_verified"] = now
    save_registry(registry, registry_path)
    return {"ok": True, "action": "agent_reactivated", "agent_id": agent_id}


def update_registry(agent_id: str, agent_name: str, project: str, db_path: str,
                    registry_path: Path = None) -> str:
    """Step 6: tems_registry.json 갱신"""
    path = registry_path or get_registry_path()
    if path is None:
        return "registry_unavailable"

    if path.exists():
        registry = json.loads(path.read_text(encoding="utf-8"))
    else:
        registry = {"version": 1, "registry_path": str(path), "projects": {}, "agents": {}}

    now = datetime.now().strftime("%Y-%m-%d")

    # 프로젝트 등록
    if project not in registry["projects"]:
        registry["projects"][project] = {"aliases": [project], "status": "active"}

    # 에이전트 등록/갱신
    if agent_id in registry["agents"]:
        agent = registry["agents"][agent_id]
        if project not in agent.get("projects", []):
            agent.setdefault("projects", []).append(project)
        agent["db_path"] = db_path
        agent["status"] = "active"
        agent["last_verified"] = now
        action = "agent_updated"
    else:
        registry["agents"][agent_id] = {
            "name": agent_name,
            "projects": [project],
            "db_path": db_path,
            "status": "active",
            "last_verified": now,
        }
        action = "agent_registered"

    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    return action


def restore_agent(agent_id: str, registry_path: Path = None) -> dict:
    """Restore — 레지스트리 기반 에이전트 인프라 복구 (데이터 보존).

    scaffold와 달리:
    - 레지스트리에서 agent 정보(name, project, db_path)를 읽어오므로 별도 인자 불필요
    - DB가 이미 존재하면 스키마만 검증/보완 (데이터 보존)
    - 템플릿은 누락 시에만 복사 (기존 커스터마이징 보존)
    - hook은 항상 재등록 (경로 갱신)
    """
    registry = load_registry(registry_path)

    if agent_id not in registry["agents"]:
        return {"ok": False, "error": f"Agent '{agent_id}' not in registry. Use 'scaffold' for new agents."}

    agent = registry["agents"][agent_id]
    db_path_str = agent.get("db_path", "")
    if not db_path_str:
        return {"ok": False, "error": f"Agent '{agent_id}' has no db_path in registry."}

    # db_path에서 cwd 역산: db_path = cwd/memory/error_logs.db
    db_path = Path(db_path_str)
    cwd = db_path.parent.parent  # memory/ 의 부모

    if not cwd.exists():
        return {"ok": False, "error": f"Agent directory does not exist: {cwd}"}

    now = datetime.now().strftime("%Y-%m-%d")
    actions = []

    # 1. Marker — 누락 시 생성, 존재 시 검증
    actions.append(create_marker(cwd, agent_id, force=False))

    # 2. Directories
    actions.extend(create_directories(cwd))

    # 3. Database — 존재하면 스키마 검증만, 없으면 생성
    actions.append(create_database(cwd, force=False))

    # 4. Gitignore
    actions.append(install_gitignore(cwd, force=False))

    # 5. Templates — 누락 시에만 복사
    actions.extend(copy_templates(cwd, force=False))

    # 6. Hook — 항상 재등록 (경로 갱신 보장)
    actions.extend(register_hook(cwd))

    # 7. Registry last_verified 갱신
    agent["last_verified"] = now
    save_registry(registry, registry_path)
    actions.append("registry_verified")

    return {"ok": True, "agent_id": agent_id, "cwd": str(cwd), "actions": actions}


def main():
    parser = argparse.ArgumentParser(description="TEMS Agent Management")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scaffold (기존 기능)
    p_scaffold = subparsers.add_parser("scaffold", help="신규 에이전트 환경 구축")
    p_scaffold.add_argument("--agent-id", required=True)
    p_scaffold.add_argument("--agent-name", required=True)
    p_scaffold.add_argument("--project", required=True)
    p_scaffold.add_argument("--cwd", required=True)
    p_scaffold.add_argument("--force", action="store_true")

    # restore (신규)
    p_restore = subparsers.add_parser("restore", help="레지스트리 기반 에이전트 인프라 복구 (데이터 보존)")
    p_restore.add_argument("--agent-id", required=True)

    # add
    p_add = subparsers.add_parser("add", help="기존 에이전트에 프로젝트 추가")
    p_add.add_argument("--agent-id", required=True)
    p_add.add_argument("--project", required=True)

    # rename
    p_rename = subparsers.add_parser("rename", help="프로젝트 이름 변경")
    p_rename.add_argument("--old", required=True)
    p_rename.add_argument("--new", required=True)

    # retire
    p_retire = subparsers.add_parser("retire", help="에이전트 은퇴")
    p_retire.add_argument("--agent-id", required=True)

    # reactivate
    p_reactivate = subparsers.add_parser("reactivate", help="에이전트 재활성화")
    p_reactivate.add_argument("--agent-id", required=True)

    args = parser.parse_args()

    if args.command == "scaffold":
        cwd = Path(args.cwd).resolve()
        actions = []
        try:
            actions.append(create_marker(cwd, args.agent_id, args.force))
            actions.extend(create_directories(cwd))
            actions.append(create_database(cwd, args.force))
            actions.append(install_gitignore(cwd, args.force))
            actions.extend(copy_templates(cwd, args.force))
            actions.extend(register_hook(cwd))
            db_path = str(cwd / "memory" / "error_logs.db")
            actions.append(update_registry(args.agent_id, args.agent_name, args.project, db_path))
            result = {"ok": True, "agent_id": args.agent_id, "actions": actions}
        except Exception as e:
            result = {"ok": False, "error": str(e), "actions": actions}

    elif args.command == "restore":
        try:
            result = restore_agent(args.agent_id)
        except Exception as e:
            result = {"ok": False, "error": str(e)}

    elif args.command == "add":
        result = add_project_to_agent(args.agent_id, args.project)

    elif args.command == "rename":
        result = rename_project(args.old, args.new)

    elif args.command == "retire":
        result = retire_agent(args.agent_id)

    elif args.command == "reactivate":
        result = reactivate_agent(args.agent_id)

    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
