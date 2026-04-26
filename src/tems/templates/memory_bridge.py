"""
TEMS Memory Bridge — AutoMemory feedback -> TEMS 자동 브릿지 (안전망)
PostToolUse hook: memory/ 폴더에 feedback 타입 Write 감지 -> TEMS DB 자동 등록
"""

import json
import sys
import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime


def _resolve_memory_dir() -> Path:
    """동적 탐지 — 우선순위: env var > marker 순회 > 현재 디렉토리.

    환경변수 TEMS_MEMORY_DIR 이 설정되어 있으면 그 경로를 우선 사용.
    없으면 __file__ 상위 경로를 순회하며 .claude/tems_agent_id 마커를 찾고,
    발견되면 그 디렉토리의 memory/ 를 반환. 없으면 __file__ 폴더 자체를 반환.
    """
    env = os.environ.get("TEMS_MEMORY_DIR", "").strip()
    if env:
        return Path(env)
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        marker = cur / ".claude" / "tems_agent_id"
        if marker.exists():
            return cur / "memory"
        cur = cur.parent
    return Path(__file__).resolve().parent  # fallback: 자기 자신 (memory/*.py)


MEMORY_DIR = _resolve_memory_dir()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_logs.db")


def parse_memory_file(file_path: str) -> dict | None:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.+)", content, re.DOTALL)
    if not match:
        return None
    frontmatter_text = match.group(1)
    body = match.group(2).strip()
    meta = {}
    for line in frontmatter_text.split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return {"name": meta.get("name", ""), "description": meta.get("description", ""), "type": meta.get("type", ""), "body": body}


def classify_rule(parsed: dict) -> str | None:
    if parsed["type"] != "feedback":
        return None
    combined = (parsed["body"] + " " + parsed["description"]).lower()
    tgl_signals = ["금지", "하지 마", "절대", "never", "don't", "do not", "prohibited", "사용하지"]
    for sig in tgl_signals:
        if sig in combined:
            return "TGL"
    return "TCL"


def extract_keywords(parsed: dict) -> str:
    text = parsed["body"] + " " + parsed["description"]
    stopwords = {"이", "그", "저", "을", "를", "에", "의", "로", "은", "는", "and", "the", "is", "a", "to", "in", "for", "of", "that", "why", "how", "apply", "when"}
    words = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", text)
    seen = set()
    unique = []
    for w in words:
        wl = w.lower()
        if wl not in seen and wl not in stopwords:
            seen.add(wl)
            unique.append(w)
    return " ".join(unique[:15])


def extract_tags(parsed: dict) -> str:
    text = parsed["body"] + " " + parsed["description"]
    words = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", text)
    seen = set()
    tags = []
    for w in words[:20]:
        wl = w.lower()
        if wl not in seen and len(wl) <= 15:
            seen.add(wl)
            tags.append(wl)
    return ",".join(tags[:8])


def bridge_to_tems(parsed: dict) -> dict:
    category = classify_rule(parsed)
    if not category:
        return {"ok": False, "reason": "not-feedback"}
    lines = parsed["body"].split("\n")
    core_rule = ""
    for line in lines:
        line = line.strip()
        if line and not line.startswith("**Why:") and not line.startswith("**How to apply:"):
            if not core_rule:
                core_rule = line
    if not core_rule:
        core_rule = parsed["body"][:200]
    triggers = extract_keywords(parsed)
    tags = extract_tags(parsed)
    if not os.path.exists(DB_PATH):
        return {"ok": False, "reason": f"DB not found: {DB_PATH}"}
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("SELECT id FROM memory_logs WHERE correction_rule = ?", (core_rule,))
    if cur.fetchone():
        conn.close()
        return {"ok": False, "reason": "duplicate"}
    cur.execute("SELECT id, keyword_trigger FROM memory_logs WHERE category = ?", (category,))
    for row in cur.fetchall():
        existing_kw = set(row[1].split())
        new_kw = set(triggers.split())
        if existing_kw and new_kw:
            overlap = len(existing_kw & new_kw) / max(len(existing_kw), len(new_kw))
            if overlap > 0.7:
                conn.close()
                return {"ok": False, "reason": f"similar-rule-exists(id={row[0]})"}
    source_tag = f"source:memory-bridge:{parsed['name']}"
    full_tags = f"{tags},{source_tag}" if tags else source_tag

    cur.execute("""
        INSERT INTO memory_logs (timestamp, category, context_tags, keyword_trigger, correction_rule, action_taken, result, severity, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, category, full_tags, triggers, core_rule, "auto-registered", "pending", "info", core_rule[:120]))
    rule_id = cur.lastrowid
    cur.execute("INSERT INTO rule_health (rule_id, ths_score, status) VALUES (?, 0.5, 'warm')", (rule_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "rule_id": rule_id, "category": category, "rule": core_rule[:80]}


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        return
    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})
    if tool_name not in ("Write", "Edit"):
        return
    file_path = tool_input.get("file_path", "")
    if str(MEMORY_DIR) not in file_path.replace("/", "\\"):
        return
    if file_path.endswith("MEMORY.md"):
        return
    parsed = parse_memory_file(file_path)
    if not parsed or parsed["type"] != "feedback":
        return
    result = bridge_to_tems(parsed)
    if result["ok"]:
        print(f"[TEMS Bridge] {result['category']} #{result['rule_id']} auto-registered from AutoMemory: {result['rule']}")

if __name__ == "__main__":
    main()
