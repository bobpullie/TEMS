"""compliance_tracker.py — PostToolUse violation/compliance counting.

Hook contract (verified against src/tems/templates/compliance_tracker.py):
- Reads: tool_name, tool_input, tool_response from stdin JSON.
- active_guards.json schema: {"guards": [...]} — dict with "guards" key (NOT bare list).
  Each guard: {"rule_id": int, "failure_signature": str, "remaining_checks": int, ...}
- Violation detection priority:
    1) tool_pattern — regex against build_match_target (tool_name + input fields)
    2) failure_signature — regex against extract_response_text (reads tool_response["output"]
       or tool_response["stdout"], NOT "stderr")
    3) FORBIDDEN heuristic — fallback when neither tool_pattern nor failure_signature present;
       only fires for MUTATING_TOOLS and needs 3+ distinct tokens from correction_rule body
- Increment: INSERT OR REPLACE / ON CONFLICT upsert into rule_health.violation_count.
  No pre-existing rule_health row required.
- remaining_checks: must be > 0, otherwise guard window expires and compliance fires instead.
- MEMORY_DIR resolves from Path(__file__).resolve().parent (hook file in memory/ dir).
  active_guards.json lives at memory/active_guards.json.

Plan deviations:
- active_guards.json must use {"guards": [...]} wrapper (plan used bare list).
- Guard uses "failure_signature" key (not "forbidden": [...]).
- tool_response must use "stdout" (not "stderr") for failure_signature matching.
- remaining_checks field needed to prevent immediate window expiry.
- No "forbidden:..." context_tag needed; rule_id + failure_signature is sufficient.
"""
import json
from tests.hooks.conftest import run_hook, insert_rule, query_db


def test_violation_increments_count(agent_dir):
    """A PostToolUse event matching a TGL's failure_signature increments
    rule_health.violation_count for that rule."""
    rid = insert_rule(
        agent_dir,
        context_tags=["classification:TGL-D"],
        action_taken="[TGL-D] missing pkg",
        result="case: import without verification",
        correction_rule="run pip show before import",
        keyword_trigger="ModuleNotFoundError import verify",
        category="TGL",
        severity="error",
    )

    # Seed an active guard for this rule.
    # Schema: {"guards": [...]} with failure_signature for response-text matching.
    # remaining_checks > 0 so the window does not expire on this tick.
    guards_path = agent_dir / "memory" / "active_guards.json"
    guards_path.write_text(json.dumps({
        "guards": [{
            "rule_id": rid,
            "failure_signature": "ModuleNotFoundError",
            "remaining_checks": 5,
        }]
    }), encoding="utf-8")

    # tool_response["stdout"] is what extract_response_text reads first.
    # failure_signature is matched against that text.
    event = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": str(agent_dir),
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "python -c 'import nope'"},
        "tool_response": {"stdout": "ModuleNotFoundError: No module named 'nope'"},
    }
    run_hook(agent_dir, "compliance_tracker.py", event)

    rows = query_db(
        agent_dir,
        "SELECT violation_count FROM rule_health WHERE rule_id = ?",
        (rid,),
    )
    assert rows and rows[0]["violation_count"] >= 1, rows
