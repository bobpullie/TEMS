"""tool_gate_hook.py — PreToolUse Layer 2 deny contract.

Hook contract (verified against src/tems/templates/tool_gate_hook.py):
- Reads: tool_name, tool_input.command (and other keys) from stdin JSON.
- Rule selection: category='TGL', context_tags LIKE '%tool_pattern:%',
  then classification tag == 'TGL-T'.
- Tag keys: 'classification:TGL-T' and 'tool_pattern:<regex>' (singular).
  The plan's 'tgl_classification' and 'tool_patterns' keys are WRONG.
- Pattern matching: tool_pattern value is compiled as re.IGNORECASE regex,
  matched against "<tool_name> | command=<cmd>" string.
- Severity: only 'critical' triggers deny; 'warning' emits text alert only.
- No project scope filter (unlike preflight hook).
- Deny output: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
    "permissionDecision": "deny", "permissionDecisionReason": "..."}}
- No deny: hook emits plaintext <tgl-tool-alert> or nothing at all.
"""
from tests.hooks.conftest import run_hook, insert_rule


def test_tgl_t_critical_denies_matching_tool_call(agent_dir):
    """TGL-T with severity=critical and matching tool_pattern must produce
    permissionDecision=deny JSON (Layer 2 hard block).

    Deviations from plan:
    - context_tags uses 'classification:TGL-T' not 'tgl_classification:TGL-T'
    - context_tags uses 'tool_pattern:rm -rf' not 'tool_patterns:rm -rf /'
      (singular key; value is a regex matched against the command string)
    - No 'project:TestProject' tag needed — hook has no project scope filter
    """
    insert_rule(
        agent_dir,
        context_tags=["classification:TGL-T", "tool_pattern:rm -rf"],
        action_taken="[TGL-T] forbidden bash",
        result="case: destructive rm -rf",
        correction_rule="never run rm -rf on root paths",
        keyword_trigger="rm rf root destructive",
        category="TGL",
        severity="critical",
    )
    event = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": str(agent_dir),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /tmp/foo"},
    }
    out = run_hook(agent_dir, "tool_gate_hook.py", event)
    hook_out = out.get("hookSpecificOutput", {})
    assert hook_out.get("permissionDecision") == "deny", out
    assert "TGL" in hook_out.get("permissionDecisionReason", ""), out


def test_tgl_t_warning_does_not_deny(agent_dir):
    """severity=warning -> no deny; hook emits plaintext alert or nothing.

    Deviations from plan: same tag key corrections as the critical test.
    """
    insert_rule(
        agent_dir,
        context_tags=["classification:TGL-T", "tool_pattern:cat /etc/passwd"],
        action_taken="[TGL-T] warn",
        result="case: read sensitive file",
        correction_rule="prefer not to cat passwd",
        keyword_trigger="cat passwd warn",
        category="TGL",
        severity="warning",
    )
    event = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": str(agent_dir),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cat /etc/passwd"},
    }
    out = run_hook(agent_dir, "tool_gate_hook.py", event)
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision != "deny", f"Warning should not deny, got: {out}"
    # Prove the rule actually fired (not a vacuous pass on rule-not-selected).
    raw = out.get("_raw_stdout", "")
    assert "<tgl-tool-alert>" in raw, f"Expected warning alert in stdout, got: {raw!r}"
    assert "cat /etc/passwd" in raw, f"Expected pattern echoed in alert body, got: {raw!r}"
