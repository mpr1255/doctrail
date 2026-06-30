import sqlite3

from doctrail.core_runtime.commands import _build_icr_override_query, _persist_icr_sample


def test_icr_override_query_quotes_key_values_and_identifier():
    original_query = """
        SELECT 'O''Reilly' AS "case key"
        UNION ALL
        SELECT 'plain' AS "case key"
    """

    override_query = _build_icr_override_query(
        original_query=original_query,
        key_col="case key",
        key_values=["O'Reilly"],
    )

    rows = sqlite3.connect(":memory:").execute(override_query).fetchall()

    assert rows == [("O'Reilly",)]


def test_persist_icr_sample_keeps_zero_key(tmp_path):
    db_path = tmp_path / "icr.db"

    _persist_icr_sample(
        db_path=str(db_path),
        enrichment_name="sentiment",
        rows=[{"id": 0}, {"id": 1}],
        seed=None,
        sample_size=2,
        stratify_by=None,
        key_column="id",
    )

    with sqlite3.connect(db_path) as conn:
        keys = [
            row[0]
            for row in conn.execute(
                "SELECT key_value FROM _icr_samples ORDER BY key_value"
            ).fetchall()
        ]

    assert keys == ["0", "1"]
