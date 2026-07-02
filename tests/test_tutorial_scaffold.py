import sqlite3
import re
import yaml
from collections import Counter
from pathlib import Path

from click.testing import CliRunner

from doctrail.cli import cli


def _alpha_from_report(output):
    match = re.search(r"Krippendorff's alpha:\s+(-?\d+(?:\.\d+)?)", output)
    assert match, output
    return float(match.group(1))


def test_init_test_replay_pipeline_is_offline(mocker, tmp_path):
    mocker.stopall()
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "test"])
        assert init_result.exit_code == 0, init_result.output
        assert Path(".doctrail/enrichments/test.yml").exists()
        assert Path(".doctrail/enrichments/securitization.yml").exists()
        assert Path(".doctrail/enrichments/mentions_climate.yml").exists()
        assert Path(".doctrail/enrichments/optimism.yml").exists()
        assert Path(".doctrail/replay/test.jsonl").exists()
        assert Path(".doctrail/replay/securitization.jsonl").exists()
        assert Path(".doctrail/replay/mentions_climate.jsonl").exists()
        assert Path(".doctrail/replay/optimism.jsonl").exists()
        assert Path("data/federalist_49.pdf").exists()
        un_speech_paths = list(Path("data/un_speeches").glob("*_general_debate_2023.*"))
        assert len(un_speech_paths) == 10
        assert Counter(path.suffix for path in un_speech_paths) == {
            ".pdf": 4,
            ".docx": 3,
            ".html": 3,
        }
        assert Path("out/database.db").exists()
        test_config = yaml.safe_load(Path(".doctrail/enrichments/test.yml").read_text())
        assert "consensus_author" not in test_config["input"]["input_columns"]

        db_path = Path("out/database.db")
        with sqlite3.connect(db_path) as conn:
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            federalist_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE corpus = 'federalist'"
            ).fetchone()[0]
            consensus_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE consensus_author IS NOT NULL"
            ).fetchone()[0]
            short_content = conn.execute(
                "SELECT filename, length(raw_content) FROM documents WHERE length(raw_content) <= 500 ORDER BY filename"
            ).fetchall()
        assert doc_count == 28
        assert federalist_count == 18
        assert consensus_count == 18
        assert short_content == []

        run_result = runner.invoke(cli, ["run", "test"])
        assert run_result.exit_code == 0, run_result.output
        with sqlite3.connect(db_path) as conn:
            test_rows = conn.execute(
                "SELECT COUNT(*) FROM _enrichments WHERE enrichment_name = 'test'"
            ).fetchone()[0]
            view_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(v_documents_enriched)").fetchall()
            }
        assert test_rows == 54
        assert {"author", "fear_of_disunion", "consensus_author"}.issubset(view_columns)

        securitization_result = runner.invoke(cli, ["run", "securitization", "--limit", "100"])
        assert securitization_result.exit_code == 0, securitization_result.output
        with sqlite3.connect(db_path) as conn:
            securitization_docs = conn.execute(
                """
                SELECT COUNT(DISTINCT key_value)
                FROM _enrichments
                WHERE enrichment_name = 'securitization'
                  AND field_name = 'securitizes'
                """
            ).fetchone()[0]
            gate_true_docs = conn.execute(
                """
                SELECT COUNT(*)
                FROM v_documents_enriched
                WHERE corpus = 'un_speeches'
                  AND securitizes = 'true'
                  AND securitized_issue IS NOT NULL
                  AND intensity IS NOT NULL
                """
            ).fetchone()[0]
            gate_false_docs = conn.execute(
                """
                SELECT COUNT(*)
                FROM v_documents_enriched
                WHERE corpus = 'un_speeches'
                  AND securitizes = 'false'
                  AND securitized_issue IS NULL
                  AND intensity IS NULL
                """
            ).fetchone()[0]
        assert securitization_docs == 10
        assert gate_true_docs == 3
        assert gate_false_docs == 7

        country_mentions_result = runner.invoke(cli, ["run", "country_mentions"])
        assert country_mentions_result.exit_code == 0, country_mentions_result.output
        country_view_result = runner.invoke(cli, ["view", "spec", "country_mentions"])
        assert country_view_result.exit_code == 0, country_view_result.output
        with sqlite3.connect(db_path) as conn:
            country_view_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(v_country_mentions)").fetchall()
            }
        assert "country" in country_view_columns
        assert "mention_mentioned_country" in country_view_columns
        assert "country:1" not in country_view_columns

        icr_result = runner.invoke(
            cli,
            ["icr", "country_stance", "-m", "replay/coder-a", "-m", "replay/coder-b"],
        )
        assert icr_result.exit_code == 0, icr_result.output
        assert "No pricing data for model 'replay/" not in icr_result.output
        assert "Unknown model replay/" not in icr_result.output

        report_result = runner.invoke(cli, ["icr-report", "--field", "stance"])
        assert report_result.exit_code == 0, report_result.output
        assert "ICR report: stance" in report_result.output
        assert "Krippendorff's alpha:" in report_result.output
        assert "Pairwise comparisons:" in report_result.output
        assert "agreement=" in report_result.output
        assert "kappa=" in report_result.output
        assert "Krippendorff's alpha: error" not in report_result.output
        assert "package not installed" not in report_result.output
        country_alpha = _alpha_from_report(report_result.output)
        assert 0.45 < country_alpha < 0.75

        climate_icr_result = runner.invoke(
            cli,
            ["icr", "mentions_climate", "-m", "replay/coder-a", "-m", "replay/coder-b"],
        )
        assert climate_icr_result.exit_code == 0, climate_icr_result.output
        climate_report_result = runner.invoke(cli, ["icr-report", "--field", "mentions_climate"])
        assert climate_report_result.exit_code == 0, climate_report_result.output
        climate_alpha = _alpha_from_report(climate_report_result.output)
        assert climate_alpha > 0.7

        optimism_icr_result = runner.invoke(
            cli,
            ["icr", "optimism", "-m", "replay/coder-a", "-m", "replay/coder-b"],
        )
        assert optimism_icr_result.exit_code == 0, optimism_icr_result.output
        optimism_report_result = runner.invoke(cli, ["icr-report", "--field", "optimism"])
        assert optimism_report_result.exit_code == 0, optimism_report_result.output
        optimism_alpha = _alpha_from_report(optimism_report_result.output)
        assert optimism_alpha < 0.45

        pivot_result = runner.invoke(
            cli,
            ["view", "pivot", "icr_optimism", "-e", "optimism", "--by-model"],
        )
        assert pivot_result.exit_code == 0, pivot_result.output
        assert "Created view: v_icr_optimism" in pivot_result.output
        with sqlite3.connect(db_path) as conn:
            pivot_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(v_icr_optimism)").fetchall()
            }
            pivot_rows = conn.execute("SELECT COUNT(*) FROM v_icr_optimism").fetchone()[0]
            disagreement_rows = conn.execute(
                "SELECT COUNT(*) FROM v_icr_optimism WHERE m1_optimism != m2_optimism"
            ).fetchone()[0]

        assert {"m1_optimism", "m2_optimism", "country", "raw_content"}.issubset(pivot_columns)
        assert pivot_rows == 10
        assert disagreement_rows >= 7


def test_init_test_fed_replay_pipeline_is_offline(mocker, tmp_path):
    mocker.stopall()
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "test", "fed"])
        assert init_result.exit_code == 0, init_result.output
        assert Path("data/federalist_49.pdf").exists()
        assert not Path("data/un_speeches").exists()
        assert Path(".doctrail/enrichments/test.yml").exists()
        assert not Path(".doctrail/enrichments/securitization.yml").exists()
        assert Path(".doctrail/replay/test.jsonl").exists()
        assert not Path(".doctrail/replay/securitization.jsonl").exists()
        assert Path("out/database.db").exists()

        db_path = Path("out/database.db")
        with sqlite3.connect(db_path) as conn:
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            federalist_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE corpus = 'federalist'"
            ).fetchone()[0]
        assert doc_count == 18
        assert federalist_count == 18

        run_result = runner.invoke(cli, ["run", "test"])
        assert run_result.exit_code == 0, run_result.output
        with sqlite3.connect(db_path) as conn:
            test_rows = conn.execute(
                "SELECT COUNT(*) FROM _enrichments WHERE enrichment_name = 'test'"
            ).fetchone()[0]
            view_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(v_documents_enriched)").fetchall()
            }
        assert test_rows == 54
        assert {"author", "fear_of_disunion", "consensus_author"}.issubset(view_columns)


def test_init_test_econ_threat_replay_pipeline_is_offline(mocker, tmp_path):
    mocker.stopall()
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "test", "econ-threat"])
        assert init_result.exit_code == 0, init_result.output
        gt_paths = list(Path("data").glob("gt_*.txt"))
        assert len(gt_paths) == 10
        assert not list(Path("data").glob("federalist_*"))
        assert not Path("data/un_speeches").exists()
        assert Path(".doctrail/enrichments/econ_threat.yml").exists()
        assert Path(".doctrail/replay/econ_threat.jsonl").exists()
        assert Path(".doctrail/views/econ_threat.yml").exists()
        assert not Path(".doctrail/enrichments/test.yml").exists()
        assert Path("out/database.db").exists()

        db_path = Path("out/database.db")
        with sqlite3.connect(db_path) as conn:
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert doc_count == 10

        run_result = runner.invoke(cli, ["run", "econ_threat"])
        assert run_result.exit_code == 0, run_result.output
        with sqlite3.connect(db_path) as conn:
            translated = conn.execute(
                "SELECT COUNT(*) FROM _enrichments WHERE enrichment_name = 'econ_threat' AND field_name = 'english_translation'"
            ).fetchone()[0]
        assert translated == 10

        view_result = runner.invoke(cli, ["view", "spec", "econ_threat"])
        assert view_result.exit_code == 0, view_result.output
        with sqlite3.connect(db_path) as conn:
            view_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(v_econ_threat)").fetchall()
            }
            country_rows = conn.execute("SELECT COUNT(*) FROM v_econ_threat").fetchone()[0]
            distinct_docs = conn.execute(
                "SELECT COUNT(DISTINCT filename) FROM v_econ_threat"
            ).fetchone()[0]
            multi_country_docs = conn.execute(
                "SELECT COUNT(*) FROM (SELECT filename FROM v_econ_threat GROUP BY filename HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        # One editorial explodes into many country-editorial rows.
        assert {"filename", "english_translation", "country", "econ_threat", "quote"}.issubset(view_columns)
        assert country_rows == 45
        assert distinct_docs == 10
        assert multi_country_docs == 10


def test_init_test_un_replay_pipeline_is_offline(mocker, tmp_path):
    mocker.stopall()
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "test", "un"])
        assert init_result.exit_code == 0, init_result.output
        un_speech_paths = list(Path("data/un_speeches").glob("*_general_debate_2023.*"))
        assert len(un_speech_paths) == 10
        assert not list(Path("data").glob("federalist_*"))
        assert Path(".doctrail/enrichments/securitization.yml").exists()
        assert not Path(".doctrail/enrichments/test.yml").exists()
        assert Path(".doctrail/replay/securitization.jsonl").exists()
        assert not Path(".doctrail/replay/test.jsonl").exists()
        assert Path("out/database.db").exists()

        db_path = Path("out/database.db")
        with sqlite3.connect(db_path) as conn:
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            un_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE corpus = 'un_speeches'"
            ).fetchone()[0]
        assert doc_count == 10
        assert un_count == 10

        securitization_result = runner.invoke(cli, ["run", "securitization", "--limit", "100"])
        assert securitization_result.exit_code == 0, securitization_result.output
        with sqlite3.connect(db_path) as conn:
            securitization_docs = conn.execute(
                """
                SELECT COUNT(DISTINCT key_value)
                FROM _enrichments
                WHERE enrichment_name = 'securitization'
                  AND field_name = 'securitizes'
                """
            ).fetchone()[0]
            gate_true_docs = conn.execute(
                """
                SELECT COUNT(*)
                FROM v_documents_enriched
                WHERE corpus = 'un_speeches'
                  AND securitizes = 'true'
                  AND securitized_issue IS NOT NULL
                  AND intensity IS NOT NULL
                """
            ).fetchone()[0]
            gate_false_docs = conn.execute(
                """
                SELECT COUNT(*)
                FROM v_documents_enriched
                WHERE corpus = 'un_speeches'
                  AND securitizes = 'false'
                  AND securitized_issue IS NULL
                  AND intensity IS NULL
                """
            ).fetchone()[0]
        assert securitization_docs == 10
        assert gate_true_docs == 3
        assert gate_false_docs == 7
