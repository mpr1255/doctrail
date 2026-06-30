#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["click", "pytest", "questionary"]
# ///
"""
Tests for the doctrail init command (new current-directory design).
"""
import os
import builtins
from pathlib import Path

import pytest
from click.testing import CliRunner

from doctrail.cli import cli


class TestInitCommand:
    """Test the init wizard (current directory design)."""

    def test_init_creates_doctrail_folder(self, tmp_path):
        """Test that init creates .doctrail/ structure in current directory."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Use -y flag to skip interactive prompts
            result = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test-key'])

            print(f"Output: {result.output}")
            print(f"Exit code: {result.exit_code}")
            if result.exception:
                import traceback
                traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)

            assert result.exit_code == 0

            # Check .doctrail directory was created
            doctrail_dir = Path(".doctrail")
            assert doctrail_dir.exists()
            assert Path("data").exists()

            # Check config.yml exists
            config_path = doctrail_dir / "config.yml"
            assert config_path.exists()
            config_content = config_path.read_text()
            assert "database:" in config_content
            assert "all_docs:" in config_content

            # Check enrichments directory exists
            enrichments_dir = doctrail_dir / "enrichments"
            assert enrichments_dir.exists()

            # Check default enrichment was created
            language_yml = enrichments_dir / "language.yml"
            assert language_yml.exists()
            language_content = language_yml.read_text()
            assert "Language detection usually needs only a short prefix" in language_content
            assert "Codebook:" in language_content

            # Check .env was created in root
            assert Path(".env").exists()
            assert "OPENAI_API_KEY=sk-test-key" in Path(".env").read_text()

            # Check .gitignore was created
            assert Path(".gitignore").exists()
            gitignore_content = Path(".gitignore").read_text()
            assert ".env" in gitignore_content
            assert "data/" in gitignore_content
            assert "out/" in gitignore_content

    def test_init_with_gemini_provider(self, tmp_path):
        """Test init with Gemini provider."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, [
                'init', '-y',
                '--api-key', 'AIza-test-key',
                '--provider', 'gemini'
            ])

            assert result.exit_code == 0

            config_content = Path(".doctrail/config.yml").read_text()
            env_content = Path(".env").read_text()

            assert "gemini-1.5-flash" in config_content
            assert "GOOGLE_API_KEY=AIza-test-key" in env_content

    def test_init_with_specific_enrichments(self, tmp_path):
        """Test init with specific enrichments selected."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, [
                'init', '-y',
                '--api-key', 'sk-test',
                '-e', 'language',
                '-e', 'summarize'
            ])

            print(f"Output: {result.output}")
            assert result.exit_code == 0

            enrichments_dir = Path(".doctrail/enrichments")
            assert (enrichments_dir / "language.yml").exists()
            assert (enrichments_dir / "summarize.yml").exists()

    def test_init_shows_next_steps(self, tmp_path):
        """Test that init output includes clear next steps."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'])

            assert result.exit_code == 0
            assert "Next steps:" in result.output
            assert "doctrail ingest" in result.output
            assert "doctrail enrich" in result.output

    def test_init_detects_env_api_key(self, tmp_path):
        """Test that init detects API key from environment."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Set env var and use -y to auto-accept
            result = runner.invoke(
                cli,
                ['init', '-y'],
                env={'OPENAI_API_KEY': 'sk-from-env'}
            )

            assert result.exit_code == 0
            assert "Using API key from environment" in result.output

    def test_init_prefers_project_env_file_over_inherited_environment(self, tmp_path):
        """Project-local .env should beat an inherited shell key."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path(".env").write_text("OPENAI_API_KEY=sk-local-project\n")

            result = runner.invoke(
                cli,
                ['init', '-y'],
                env={'OPENAI_API_KEY': 'sk-global-shell'},
            )

            assert result.exit_code == 0
            assert "Using API key from .env file" in result.output
            assert "OPENAI_API_KEY=sk-local-project" in Path(".env").read_text()

    def test_init_preserves_existing_env_file_entries(self, tmp_path):
        """Init should update the provider key without deleting unrelated .env lines."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path(".env").write_text(
                "# local settings\n"
                "ANTHROPIC_API_KEY=sk-ant-existing\n"
                "CUSTOM_SETTING=keep-me\n"
            )

            result = runner.invoke(
                cli,
                ['init', '-y', '--provider', 'openai', '--api-key', 'sk-new-openai'],
            )

            assert result.exit_code == 0
            env_content = Path(".env").read_text()
            assert "# local settings" in env_content
            assert "ANTHROPIC_API_KEY=sk-ant-existing" in env_content
            assert "CUSTOM_SETTING=keep-me" in env_content
            assert "OPENAI_API_KEY=sk-new-openai" in env_content

    def test_init_completes_partial_gitignore(self, tmp_path):
        """Init should add each missing ignore pattern independently."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path(".gitignore").write_text(".env\n")

            result = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'])

            assert result.exit_code == 0
            gitignore_lines = Path(".gitignore").read_text().splitlines()
            assert ".env" in gitignore_lines
            assert "*.db" in gitignore_lines
            assert "*.db-journal" in gitignore_lines
            assert ".doctrail/" in gitignore_lines

    def test_init_accepts_anthropic_provider(self, tmp_path):
        """Init should expose providers supported by the runtime model layer."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                cli,
                ['init', '-y', '--provider', 'anthropic', '--api-key', 'sk-ant-test'],
            )

            assert result.exit_code == 0
            assert "claude-3-5-haiku-latest" in Path(".doctrail/config.yml").read_text()
            assert "ANTHROPIC_API_KEY=sk-ant-test" in Path(".env").read_text()

    def test_init_fallback_selection_reports_invalid_numbers(self, tmp_path, monkeypatch):
        """The non-questionary fallback should fail cleanly on invalid selections."""
        runner = CliRunner()
        original_import = builtins.__import__

        def import_without_questionary(name, *args, **kwargs):
            if name == "questionary":
                raise ImportError("questionary unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_without_questionary)

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                cli,
                ['init', '--api-key', 'sk-test'],
                input="project\n./data/\n./out/project.db\n999\n",
            )

            assert result.exit_code != 0
            assert "Invalid enrichment selection: 999" in result.output

    def test_init_reinitialize_prompts(self, tmp_path):
        """Test that init asks for confirmation if already initialized."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # First init
            result = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'])
            assert result.exit_code == 0

            # Second init should prompt (and abort without input)
            result = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'], input='n\n')
            assert "Aborted" in result.output or "already initialized" in result.output

    def test_init_provider_flag_wins_over_env_key(self, tmp_path):
        """--provider should be honoured even if another provider's key is in the env."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path(".env").write_text("OPENAI_API_KEY=sk-openai-existing\n")

            result = runner.invoke(
                cli,
                ['init', '-y', '--provider', 'anthropic'],
                env={'ANTHROPIC_API_KEY': 'sk-ant-env'},
            )

            assert result.exit_code == 0, result.output
            config_text = Path(".doctrail/config.yml").read_text()
            env_text = Path(".env").read_text()
            # Explicit --provider anthropic must win
            assert "claude-3-5-haiku-latest" in config_text, config_text
            assert "ANTHROPIC_API_KEY=sk-ant-env" in env_text
            # The pre-existing OpenAI line is left alone
            assert "OPENAI_API_KEY=sk-openai-existing" in env_text

    def test_init_reinit_backs_up_customized_config(self, tmp_path):
        """Reinit must not silently drop user edits to config.yml and enrichment YAMLs."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            first = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'])
            assert first.exit_code == 0

            config_path = Path(".doctrail/config.yml")
            enrichment_path = Path(".doctrail/enrichments/language.yml")
            original_config = config_path.read_text()
            original_enrichment = enrichment_path.read_text()
            config_path.write_text(original_config + "\n# my custom note\n")
            enrichment_path.write_text(original_enrichment + "\n# my custom note\n")

            second = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'])
            assert second.exit_code == 0

            config_backups = list(Path(".doctrail").glob("config.yml.*.bak"))
            enrichment_backups = list(Path(".doctrail/enrichments").glob("language.yml.*.bak"))

            assert config_backups, "expected a .bak file preserving custom config edits"
            assert "my custom note" in config_backups[0].read_text()
            assert enrichment_backups, "expected a .bak file preserving custom enrichment edits"
            assert "my custom note" in enrichment_backups[0].read_text()

    def test_init_docs_path_preserves_parent_dir(self, tmp_path):
        """Typing ../somewhere for docs must not get mangled into somewhere."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                cli,
                ['init', '-y', '--api-key', 'sk-test', '--docs', '../shared_data'],
            )
            assert result.exit_code == 0, result.output
            config_text = Path(".doctrail/config.yml").read_text()
            assert "documents_path: ./../shared_data/" in config_text, config_text

    def test_init_accepts_absolute_docs_path(self, tmp_path):
        """A docs folder outside the project should stay absolute in config.yml."""
        runner = CliRunner()
        external_docs = tmp_path / "external docs"
        external_docs.mkdir()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                cli,
                ['init', '-y', '--api-key', 'sk-test', '--docs', str(external_docs)],
            )
            assert result.exit_code == 0, result.output
            config_text = Path(".doctrail/config.yml").read_text()
            assert f"documents_path: {external_docs}/" in config_text, config_text

    def test_init_next_steps_include_view_command(self, tmp_path):
        """First-time users should be told how to inspect results after enrich."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ['init', '-y', '--api-key', 'sk-test'])

            assert result.exit_code == 0
            assert "doctrail view" in result.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
