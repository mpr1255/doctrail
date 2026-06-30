#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest"]
# ///
"""Tests for the declarative enrichment identity (unique index + upserts)."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from doctrail.db_operations import (
    create_pivot_view,
    create_enrichments_views,
    EnrichmentRunWriter,
    ENRICHMENT_AUDIT_TABLE,
    ENRICHMENTS_SUPERSEDED_TABLE,
    ENRICHMENTS_TABLE,
    ensure_enrichments_table,
    get_db_connection,
    persist_enrichment_result,
    plan_existing_enrichment_skips,
    write_enrichment,
    write_enrichment_projection,
)


def _fetch_rows(db_path, sql, params=()):
    with get_db_connection(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _fetch_persisted_payload(db_path):
    audit_sql = f"""
        SELECT key_value, enrichment_name, raw_json, projection_json, projection_version,
               model_used, prompt_id, full_prompt, input_tokens, output_tokens,
               estimated_cost, run_id, query_hash, project, created_at
        FROM {ENRICHMENT_AUDIT_TABLE}
        ORDER BY key_value
    """
    enrichment_sql = f"""
        SELECT key_value, enrichment_name, field_name, value, value_type, timestamp,
               model, prompt_hash, enrichment_id, run_id, query_hash, metadata, project
        FROM {ENRICHMENTS_TABLE}
        ORDER BY key_value, field_name
    """
    return {
        "audit": _fetch_rows(db_path, audit_sql),
        "enrichments": _fetch_rows(db_path, enrichment_sql),
    }


def test_enrichment_run_writer_concurrent_persists_match_per_call(tmp_path):
    per_call_db = tmp_path / "per_call.db"
    writer_db = tmp_path / "writer.db"
    payloads = [
        {
            "key_value": "k1",
            "enrichment_name": "analysis",
            "updated": {"summary": "first", "score": 1},
            "model": "gpt-4o-mini",
            "enrichment_id": "e1",
            "prompt_id": "p1",
            "full_prompt": "Prompt text",
            "usage": {"input_tokens": 10, "output_tokens": 4, "estimated_cost": 0.01},
            "run_id": "run-1",
            "query_hash": "q1",
            "project": "proj",
            "overwrite": False,
            "raw_json": '{"summary":"first","score":1}',
            "timestamp": "2026-01-01T00:00:00",
        },
        {
            "key_value": "k2",
            "enrichment_name": "analysis",
            "updated": {"summary": "second", "score": 2},
            "model": "gpt-4o-mini",
            "enrichment_id": "e2",
            "prompt_id": "p1",
            "full_prompt": "Prompt text",
            "usage": {"input_tokens": 11, "output_tokens": 5, "estimated_cost": 0.02},
            "run_id": "run-1",
            "query_hash": "q1",
            "project": "proj",
            "overwrite": False,
            "raw_json": '{"summary":"second","score":2}',
            "timestamp": "2026-01-01T00:00:01",
        },
    ]

    for payload in payloads:
        persist_enrichment_result(str(per_call_db), **payload)

    writer = EnrichmentRunWriter(str(writer_db))
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda payload: writer.persist(**payload), payloads))
    finally:
        writer.close()

    assert _fetch_persisted_payload(writer_db) == _fetch_persisted_payload(per_call_db)


def test_identity_unique_index_created(tmp_path):
    db_path = tmp_path / "identity.db"
    ensure_enrichments_table(str(db_path))

    rows = _fetch_rows(
        db_path,
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_enrichments_identity'",
    )
    assert rows, "identity unique index missing"

    # The constraint itself: same identity twice must violate
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            f"INSERT INTO {ENRICHMENTS_TABLE} (key_value, enrichment_name, field_name, value, timestamp, model, prompt_hash) "
            "VALUES ('k1', 'e', 'f', 'v', 't', 'm', 'p')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {ENRICHMENTS_TABLE} (key_value, enrichment_name, field_name, value, timestamp, model, prompt_hash) "
                "VALUES ('k1', 'e', 'f', 'v2', 't', 'm', 'p')"
            )


def test_write_enrichment_append_keeps_first_value(tmp_path):
    db_path = tmp_path / "append.db"
    write_enrichment(str(db_path), "k1", "summary_task", "summary", "first", "gpt-4o-mini", prompt_hash="p1")
    write_enrichment(str(db_path), "k1", "summary_task", "summary", "second", "gpt-4o-mini", prompt_hash="p1")

    rows = _fetch_rows(db_path, f"SELECT value FROM {ENRICHMENTS_TABLE}")
    assert [row["value"] for row in rows] == ["first"]


def test_write_enrichment_overwrite_replaces_in_place(tmp_path):
    db_path = tmp_path / "overwrite.db"
    write_enrichment(str(db_path), "k1", "summary_task", "summary", "first", "gpt-4o-mini", prompt_hash="p1")
    write_enrichment(
        str(db_path), "k1", "summary_task", "summary", "second", "gpt-4o-mini",
        prompt_hash="p1", overwrite=True,
    )

    rows = _fetch_rows(db_path, f"SELECT value FROM {ENRICHMENTS_TABLE}")
    assert [row["value"] for row in rows] == ["second"]


def test_write_enrichment_new_prompt_version_appends(tmp_path):
    db_path = tmp_path / "versions.db"
    write_enrichment(str(db_path), "k1", "summary_task", "summary", "v1", "gpt-4o-mini", prompt_hash="p1")
    write_enrichment(
        str(db_path), "k1", "summary_task", "summary", "v2", "gpt-4o-mini",
        prompt_hash="p2", overwrite=True,
    )

    rows = _fetch_rows(db_path, f"SELECT prompt_hash, value FROM {ENRICHMENTS_TABLE} ORDER BY id")
    assert [(row["prompt_hash"], row["value"]) for row in rows] == [("p1", "v1"), ("p2", "v2")]


def test_write_enrichment_normalizes_null_prompt_hash(tmp_path):
    db_path = tmp_path / "nullhash.db"
    write_enrichment(str(db_path), "k1", "summary_task", "summary", "first", "gpt-4o-mini", prompt_hash=None)
    write_enrichment(str(db_path), "k1", "summary_task", "summary", "second", "gpt-4o-mini", prompt_hash=None)

    rows = _fetch_rows(db_path, f"SELECT prompt_hash, value FROM {ENRICHMENTS_TABLE}")
    assert [(row["prompt_hash"], row["value"]) for row in rows] == [("", "first")]


def test_write_enrichment_projection_upserts(tmp_path):
    db_path = tmp_path / "projection.db"
    rows = [
        {"field_name": "summary", "value": "s1", "value_type": "string"},
        {"field_name": "score", "value": "4", "value_type": "integer"},
    ]
    written = write_enrichment_projection(
        str(db_path), "k1", "analysis", rows, "gpt-4o-mini", prompt_hash="p1",
    )
    assert written == 2

    # Append mode skips both
    written = write_enrichment_projection(
        str(db_path), "k1", "analysis", rows, "gpt-4o-mini", prompt_hash="p1",
    )
    assert written == 0

    # Overwrite mode updates both in place
    rows2 = [
        {"field_name": "summary", "value": "s2", "value_type": "string"},
        {"field_name": "score", "value": "5", "value_type": "integer"},
    ]
    written = write_enrichment_projection(
        str(db_path), "k1", "analysis", rows2, "gpt-4o-mini", prompt_hash="p1", overwrite=True,
    )
    assert written == 2

    stored = _fetch_rows(db_path, f"SELECT field_name, value FROM {ENRICHMENTS_TABLE} ORDER BY field_name")
    assert [(row["field_name"], row["value"]) for row in stored] == [("score", "5"), ("summary", "s2")]


def test_boolean_values_stored_json_canonical(tmp_path):
    db_path = tmp_path / "bools.db"
    write_enrichment(str(db_path), "k1", "flags", "relevant", True, "gpt-4o-mini", prompt_hash="p1")
    write_enrichment(str(db_path), "k2", "flags", "relevant", False, "gpt-4o-mini", prompt_hash="p1")

    rows = _fetch_rows(db_path, f"SELECT key_value, value, value_type FROM {ENRICHMENTS_TABLE} ORDER BY key_value")
    assert [(row["value"], row["value_type"]) for row in rows] == [("true", "boolean"), ("false", "boolean")]


def test_null_answer_is_persisted_and_skipped_in_all_dedupe_scopes(tmp_path):
    db_path = tmp_path / "null_answer.db"
    persist_enrichment_result(
        str(db_path),
        key_value="k1",
        enrichment_name="screen",
        updated={"rationale": None},
        model="gpt-4o-mini",
        prompt_id="p1",
        query_hash="q1",
        projection_output_fields=["rationale"],
        raw_json='{"rationale": null}',
    )

    stored = _fetch_rows(
        db_path,
        f"""
        SELECT field_name, value, value_type
        FROM {ENRICHMENTS_TABLE}
        WHERE key_value = 'k1'
        """,
    )
    assert stored == [{"field_name": "rationale", "value": None, "value_type": "null"}]

    rows = [{"rowid": 1, "sha1": "k1"}]
    base_kwargs = {
        "db_path": str(db_path),
        "rows": rows,
        "enrichment_name": "screen",
        "model": "gpt-4o-mini",
        "prompt_id": "p1",
        "key_column": "sha1",
        "output_table": None,
        "output_cols": ["rationale"],
        "separate_output_db": False,
        "source_table": "documents",
    }

    query_scope = plan_existing_enrichment_skips(
        **base_kwargs,
        dedupe_scope="query",
        query_hash="q1",
    )
    prompt_scope = plan_existing_enrichment_skips(
        **base_kwargs,
        dedupe_scope="prompt",
        query_hash="q2",
    )
    enrichment_scope = plan_existing_enrichment_skips(
        **base_kwargs,
        dedupe_scope="enrichment",
        query_hash="q2",
    )

    assert [row["key_value"] for row in query_scope] == ["k1"]
    assert [row["key_value"] for row in prompt_scope] == ["k1"]
    assert [row["key_value"] for row in enrichment_scope] == ["k1"]


def test_top_level_null_response_records_all_declared_fields_as_answered(tmp_path):
    db_path = tmp_path / "all_null.db"
    projection = persist_enrichment_result(
        str(db_path),
        key_value="k1",
        enrichment_name="screen",
        updated=None,
        model="gpt-4o-mini",
        prompt_id="p1",
        query_hash="q1",
        projection_output_fields=["answer", "reason"],
        raw_json="null",
    )

    assert projection == [
        {"field_name": "answer", "value": None, "value_type": "null", "metadata": None},
        {"field_name": "reason", "value": None, "value_type": "null", "metadata": None},
    ]

    skipped = plan_existing_enrichment_skips(
        db_path=str(db_path),
        rows=[{"rowid": 1, "sha1": "k1"}],
        enrichment_name="screen",
        model="gpt-4o-mini",
        prompt_id="p1",
        key_column="sha1",
        dedupe_scope="enrichment",
        query_hash="q2",
        output_table=None,
        output_cols=["answer", "reason"],
        separate_output_db=False,
        source_table="documents",
    )

    assert [row["key_value"] for row in skipped] == ["k1"]


def test_null_value_type_does_not_block_numeric_view_casting(tmp_path):
    db_path = tmp_path / "null_cast.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE documents (sha1 TEXT PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO documents (sha1, title) VALUES (?, ?)",
            [("k1", "Null score"), ("k2", "Numeric score")],
        )
        conn.commit()

    persist_enrichment_result(
        str(db_path),
        key_value="k1",
        enrichment_name="score_task",
        updated={"score": None},
        model="gpt-4o-mini",
        prompt_id="p1",
        query_hash="q1",
        projection_output_fields=["score"],
        raw_json='{"score": null}',
    )
    write_enrichment(
        str(db_path),
        "k2",
        "score_task",
        "score",
        5,
        "gpt-4o-mini",
        prompt_hash="p1",
    )

    result = create_pivot_view(
        str(db_path),
        "score_review",
        "score_task",
        documents_table="documents",
        fields=["score"],
        include_columns=["title"],
    )

    assert result["view_name"] == "v_score_review"
    rows = _fetch_rows(
        db_path,
        "SELECT sha1, score, typeof(score) AS score_type FROM v_score_review ORDER BY sha1",
    )
    assert rows == [
        {"sha1": "k1", "score": None, "score_type": "null"},
        {"sha1": "k2", "score": 5, "score_type": "integer"},
    ]


def test_migration_moves_duplicates_to_superseded(tmp_path):
    db_path = tmp_path / "legacy.db"

    # Build a legacy table by hand: no identity index, duplicate identities,
    # NULL prompt hashes, and Python-style booleans.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE enrichments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_value TEXT NOT NULL,
                enrichment_name TEXT NOT NULL,
                field_name TEXT NOT NULL,
                value TEXT,
                value_type TEXT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_hash TEXT,
                enrichment_id TEXT,
                run_id TEXT,
                query_hash TEXT,
                metadata TEXT,
                project TEXT
            )
        """)
        legacy_rows = [
            ("k1", "task", "field", "old", "string", "2024-01-01", "m1", None),
            ("k1", "task", "field", "newer", "string", "2024-02-01", "m1", None),
            ("k2", "task", "flag", "True", "boolean", "2024-01-01", "m1", "p1"),
        ]
        conn.executemany(
            "INSERT INTO enrichments (key_value, enrichment_name, field_name, value, value_type, timestamp, model, prompt_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            legacy_rows,
        )
        conn.commit()

    ensure_enrichments_table(str(db_path))

    live = _fetch_rows(db_path, f"SELECT key_value, value, prompt_hash FROM {ENRICHMENTS_TABLE} ORDER BY key_value")
    # Latest row (highest id) wins for the duplicated identity
    assert ("k1", "newer", "") in {(r["key_value"], r["value"], r["prompt_hash"]) for r in live}
    assert all(r["prompt_hash"] is not None for r in live)
    # Boolean normalized to JSON-canonical
    assert {(r["key_value"], r["value"]) for r in live} >= {("k2", "true")}
    assert len(live) == 2

    superseded = _fetch_rows(db_path, f"SELECT key_value, value FROM {ENRICHMENTS_SUPERSEDED_TABLE}")
    assert [(r["key_value"], r["value"]) for r in superseded] == [("k1", "old")]

    # Migration must be idempotent
    ensure_enrichments_table(str(db_path))
    assert len(_fetch_rows(db_path, f"SELECT id FROM {ENRICHMENTS_TABLE}")) == 2


def test_enriched_view_scopes_columns_per_enrichment(tmp_path):
    db_path = tmp_path / "views.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE documents (sha1 TEXT PRIMARY KEY, filename TEXT)")
        conn.execute("INSERT INTO documents VALUES ('k1', 'doc.pdf')")
        conn.commit()

    # Two enrichments share the field name "summary"; one has a unique field.
    write_enrichment(str(db_path), "k1", "task_a", "summary", "from-a", "m1", prompt_hash="p1")
    write_enrichment(str(db_path), "k1", "task_b", "summary", "from-b", "m1", prompt_hash="p1")
    write_enrichment(str(db_path), "k1", "task_b", "tone", "neutral", "m1", prompt_hash="p1")

    create_enrichments_views(str(db_path), source_table="documents")

    rows = _fetch_rows(db_path, "SELECT * FROM v_documents_enriched")
    assert len(rows) == 1
    row = rows[0]
    # Colliding field names get enrichment-prefixed columns; unique ones stay bare
    assert row["task_a_summary"] == "from-a"
    assert row["task_b_summary"] == "from-b"
    assert row["tone"] == "neutral"
    assert "summary" not in row


def test_enriched_view_latest_wins_with_id_tiebreak(tmp_path):
    db_path = tmp_path / "tiebreak.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE documents (sha1 TEXT PRIMARY KEY, filename TEXT)")
        conn.execute("INSERT INTO documents VALUES ('k1', 'doc.pdf')")
        conn.commit()

    shared_timestamp = "2026-01-01T00:00:00"
    write_enrichment(
        str(db_path), "k1", "task", "summary", "first", "m1",
        prompt_hash="p1", timestamp=shared_timestamp,
    )
    write_enrichment(
        str(db_path), "k1", "task", "summary", "second", "m1",
        prompt_hash="p2", timestamp=shared_timestamp,
    )

    create_enrichments_views(str(db_path), source_table="documents")
    rows = _fetch_rows(db_path, "SELECT summary FROM v_documents_enriched")
    assert rows == [{"summary": "second"}]
