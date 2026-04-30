"""preflight_hook.py — UserPromptSubmit rule injection contract.

Hook contract (verified from src/tems/templates/preflight_hook.py):
- Reads: data["prompt"], data["cwd"] from stdin JSON.
  session_id and transcript_path are present in the event shape but NOT read
  by the hook; only prompt and cwd are consumed.
- Output: plaintext to stdout, wrapped in <preflight-memory-check>...</preflight-memory-check>.
  run_hook() returns {"_raw_stdout": ...} for non-JSON output.
- Threshold: FTS5 BM25 match on keyword_trigger field. extract_keywords() strips
  stopwords and Korean suffixes from the prompt, then queries with prefix wildcards.
- Scope filter: filter_by_project() checks context_tags for a "project:X" tag.
  Rules with no "project:" tag in context_tags get project_tag="" which is always
  in allowed_scopes -> they always pass the filter. Used here to avoid coupling
  tests to tmp_path segment heuristics.
- Silent if no hits: format_rules() returns "" -> nothing printed -> empty stdout
  -> run_hook() returns {}.
"""
import json
import os
import subprocess
import sys

from tests.hooks.conftest import run_hook, insert_rule


def test_preflight_injects_matching_tgl(agent_dir):
    """FTS5-matching TGL rule must appear in <preflight-memory-check> block.

    Adjustment from plan: context_tags uses no "project:" prefix tag (empty list)
    so that project_tag="" passes filter_by_project unconditionally, avoiding
    dependence on tmp_path segment heuristics. The keyword_trigger contains
    "useEffect" and "closure" which appear verbatim in the prompt after
    extract_keywords() stopword removal, ensuring BM25 prefix match.
    """
    rid = insert_rule(
        agent_dir,
        context_tags=[],
        action_taken="[TGL] test guard",
        result="topological case: useEffect deps stale closure",
        correction_rule="useEffect deps must exclude rapidly-changing values",
        keyword_trigger="useEffect closure currentPrice stale",
        category="TGL",
        severity="error",
    )

    event = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": str(agent_dir),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "useEffect deps에 currentPrice 넣으면 stale closure 발생",
    }
    out = run_hook(agent_dir, "preflight_hook.py", event)
    raw = out.get("_raw_stdout", "")
    assert "preflight-memory-check" in raw, (
        f"Expected <preflight-memory-check> injection, got: {raw!r}"
    )
    assert f"#{rid}" in raw or "useEffect" in raw, (
        f"Expected rule #{rid} content in output: {raw!r}"
    )


def test_preflight_silent_below_threshold(agent_dir):
    """Unrelated prompt must produce no <preflight-memory-check> injection.

    The keyword_trigger uses a unique unmatchable token. extract_keywords()
    will pull real words from the unrelated prompt ("apple", "pie", "recipes")
    which have zero overlap with "zzz_rare_keyword_unmatchable".
    """
    insert_rule(
        agent_dir,
        context_tags=[],
        action_taken="[TGL] niche rule",
        result="niche case",
        correction_rule="niche correction rule",
        keyword_trigger="zzz_rare_keyword_unmatchable",
        category="TGL",
    )
    event = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": str(agent_dir),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "completely unrelated topic about apple pie recipes",
    }
    out = run_hook(agent_dir, "preflight_hook.py", event)
    raw = out.get("_raw_stdout", "")
    assert "preflight-memory-check" not in raw, (
        f"Expected silent preflight (no injection), got: {raw!r}"
    )


def test_preflight_never_blocks(agent_dir):
    """Empty event must produce zero output and exit 0.

    Source: preflight_hook.main() bails at `if not prompt.strip(): sys.exit(0)`
    before any DB access — empty event guarantees stdout is empty.
    run_hook() asserts returncode == 0 internally.
    """
    out = run_hook(agent_dir, "preflight_hook.py", {})
    assert out == {}, f"Empty event should produce no output, got: {out}"


def test_preflight_outermost_failure_writes_diagnostic(agent_dir):
    """Outermost ``except Exception`` must emit a diagnostic JSONL row instead
    of silently swallowing the failure (Critical #5).

    Force a hook crash by replacing memory/error_logs.db with a directory —
    sqlite3.connect() will then raise inside EnhancedPreflight, propagating to
    the outermost try in main(). We assert a row lands in
    memory/tems_diagnostics.jsonl using the same shape as
    audit_diagnostics_recent (event/exc_type/exc_msg/traceback) so the existing
    SessionStart α-layer auto-surfaces it.
    """
    db_path = agent_dir / "memory" / "error_logs.db"
    if db_path.exists():
        db_path.unlink()
    db_path.mkdir()  # opening as DB now raises OperationalError

    event = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": str(agent_dir),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "any prompt that reaches DB access",
    }
    out = run_hook(agent_dir, "preflight_hook.py", event)
    # Hook must still exit 0 (run_hook asserts) and produce no stdout.
    assert out == {}, f"Failed hook must stay silent on stdout, got: {out}"

    diag_path = agent_dir / "memory" / "tems_diagnostics.jsonl"
    assert diag_path.exists(), (
        f"Expected diagnostic log at {diag_path}, hook silently swallowed failure"
    )
    rows = [json.loads(line) for line in diag_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    matches = [r for r in rows if r.get("event") == "preflight_failure"]
    assert matches, f"No preflight_failure row in diagnostic log, got: {rows}"
    row = matches[-1]
    # Shape parity with audit_diagnostics_recent._log_diagnostic
    assert "timestamp" in row and "exc_type" in row and "exc_msg" in row and "traceback" in row, (
        f"Diagnostic row missing audit-compatible keys, got: {row}"
    )

    # End-to-end: the existing audit_diagnostics_recent SessionStart hook must
    # auto-surface the row written above. This is what makes Option-A real —
    # if format/path drifts again, this assertion catches it.
    audit_path = agent_dir / "memory" / "audit_diagnostics_recent.py"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("TEMS_REGISTRY_PATH", None)
    env.pop("TEMS_MEMORY_DIR", None)
    audit_run = subprocess.run(
        [sys.executable, str(audit_path)],
        capture_output=True, text=True, timeout=10.0,
        cwd=str(agent_dir), env=env, encoding="utf-8",
    )
    assert audit_run.returncode == 0, (
        f"audit_diagnostics_recent exited {audit_run.returncode}\n"
        f"STDERR: {audit_run.stderr}"
    )
    assert "preflight_failure" in audit_run.stdout, (
        f"audit_diagnostics_recent did not surface preflight_failure row.\n"
        f"STDOUT: {audit_run.stdout!r}"
    )
