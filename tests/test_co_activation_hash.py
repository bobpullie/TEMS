"""M1 — co_activations.prompt_hash must be deterministic across processes.

The previous implementation used Python's builtin ``hash()`` which is salted
per-process under PEP 456, so the same prompt produced different hashes on
restart, defeating the column's purpose. Verify the new ``hashlib.sha1`` path
is stable.
"""
import hashlib
import sqlite3

from tems.fts5_memory import MemoryDB
from tems.tems_engine import RuleGraph


def _seed_two_rules(db: MemoryDB) -> tuple[int, int]:
    a = db.commit_memory(
        action_taken="[TGL] rule a",
        result="seed a",
        correction_rule="rule a",
        keyword_trigger="alpha",
        context_tags=[],
        category="TGL",
    )
    b = db.commit_memory(
        action_taken="[TGL] rule b",
        result="seed b",
        correction_rule="rule b",
        keyword_trigger="beta",
        context_tags=[],
        category="TGL",
    )
    return a, b


def test_record_co_activation_uses_sha1_prompt_hash(tmp_path):
    """Two record_co_activation calls with the same prompt must store the same
    prompt_hash, and that hash must equal sha1(prompt)[:16]."""
    db_path = tmp_path / "memory" / "error_logs.db"
    db_path.parent.mkdir(parents=True)
    db = MemoryDB(str(db_path))

    rid_a, rid_b = _seed_two_rules(db)
    graph = RuleGraph(db)

    prompt = "TDD 필수 — 테스트 먼저"
    expected = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16]

    graph.record_co_activation(prompt, [rid_a, rid_b])
    graph.record_co_activation(prompt, [rid_a, rid_b])  # second call same prompt

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT prompt_hash FROM co_activations ORDER BY rowid"
        ).fetchall()

    assert len(rows) >= 2, f"Expected at least 2 co_activation rows, got: {rows}"
    hashes = {r[0] for r in rows}
    assert hashes == {expected}, (
        f"prompt_hash not deterministic — got {hashes}, expected single {{{expected}}}"
    )


def test_record_co_activation_distinguishes_different_prompts(tmp_path):
    """Different prompts must produce different prompt_hash values (no collision
    in sha1 16-hex-char prefix for these inputs)."""
    db_path = tmp_path / "memory" / "error_logs.db"
    db_path.parent.mkdir(parents=True)
    db = MemoryDB(str(db_path))

    rid_a, rid_b = _seed_two_rules(db)
    graph = RuleGraph(db)

    graph.record_co_activation("prompt one", [rid_a, rid_b])
    graph.record_co_activation("prompt two", [rid_a, rid_b])

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT prompt_hash FROM co_activations"
        ).fetchall()

    assert len(rows) == 2, (
        f"Expected 2 distinct prompt_hash values for 2 different prompts, got: {rows}"
    )
