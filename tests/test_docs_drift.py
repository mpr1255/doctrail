import importlib.util
import re
import shlex
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from doctrail.cli import cli
from doctrail.enrichment_config import prepare_enrichment_for_processing, validate_enrichment_config


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SKILL = ROOT / "skills" / "doctrail" / "SKILL.md"
PRESETS = ROOT / "src" / "doctrail" / "presets"


FENCE_RE = re.compile(r"```(?P<info>[^\n]*)\n(?P<body>.*?)```", re.DOTALL)


def _iter_fences(paths):
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in FENCE_RE.finditer(text):
            yield path, match.group("info").strip(), match.group("body").strip()


def _load_build_llms_module():
    spec = importlib.util.spec_from_file_location(
        "build_llms_full_for_test",
        ROOT / "scripts" / "build_llms_full.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _validate_enrichment(enrichment, *, context):
    errors = validate_enrichment_config(enrichment)
    assert not errors, f"{context}: {errors}"
    strategy, config_errors = prepare_enrichment_for_processing(
        enrichment,
        default_table="documents",
        sql_queries={
            "all_docs": "SELECT rowid, sha1 FROM documents ORDER BY rowid",
            "donor_docs": "SELECT rowid, sha1 FROM documents ORDER BY rowid",
            "docs_with_entities": "SELECT rowid, sha1 FROM documents ORDER BY rowid",
        },
        config_key_column="sha1",
    )
    assert strategy is not None
    assert not config_errors, f"{context}: {config_errors}"


def test_documented_yaml_snippets_validate():
    paths = [DOCS / "yaml.md", DOCS / "quickstart.md", SKILL]
    checked = 0

    for path, info, body in _iter_fences(paths):
        if not info.startswith("yaml"):
            continue
        if "fragment" in info:
            continue
        if not any(marker in body for marker in ("schema:", "enrichments:", "input:")):
            continue

        data = yaml.safe_load(body)
        assert isinstance(data, dict), f"{path}: YAML snippet must be a mapping"

        if "enrichments" in data:
            enrichments = data["enrichments"] or []
            assert isinstance(enrichments, list), f"{path}: enrichments must be a list"
            for index, enrichment in enumerate(enrichments):
                _validate_enrichment(enrichment, context=f"{path} enrichment {index}")
                checked += 1
        elif "name" in data:
            _validate_enrichment(data, context=str(path))
            checked += 1

    assert checked >= 6


def test_packaged_presets_validate_with_comments():
    from doctrail.cli.utils import _load_presets

    presets = _load_presets()
    assert len(presets) >= 8

    for name, preset in sorted(presets.items()):
        content = preset["content"]
        assert "#" in content, f"{name}: preset should include teaching comments"
        data = yaml.safe_load(content)
        assert isinstance(data, dict), f"{name}: preset YAML must be a mapping"
        _validate_enrichment(data, context=str(PRESETS / preset["filename"]))


def _extract_doctrail_args(line):
    if not line or line.startswith("#"):
        return None
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if not tokens:
        return None
    if tokens[0] == "doctrail":
        return tokens[1:]
    if len(tokens) >= 3 and tokens[0:2] == ["uv", "run"] and tokens[2] == "doctrail":
        return tokens[3:]
    return None


@pytest.mark.parametrize(
    "path",
    sorted(DOCS.glob("*.md")) + [ROOT / "README.md", SKILL],
)
def test_documented_doctrail_invocations_resolve(path):
    runner = CliRunner()
    for source_path, info, body in _iter_fences([path]):
        if not info.startswith(("bash", "sh", "console")):
            continue
        for raw_line in body.splitlines():
            args = _extract_doctrail_args(raw_line.strip())
            if args is None:
                continue
            result = runner.invoke(cli, args + ["--help"])
            assert result.exit_code == 0, (
                f"{source_path}: `{raw_line}` does not resolve against Click.\n"
                f"Output:\n{result.output}"
            )


def test_cli_help_uses_post_rename_names():
    module = _load_build_llms_module()
    forbidden_literals = [
        "SELECT * FROM run_",
        "SELECT * FROM final_",
    ]
    forbidden_patterns = [
        re.compile(r"(?<!_)enrichment_audit\b"),
        re.compile(r"(?<!_)enrichment_runs\b"),
        re.compile(r"(?<!_)enrichment_run_items\b"),
        re.compile(r"(?<!_)enrichments table\b"),
        re.compile(r"(?<!v_)run_[A-Za-z][A-Za-z0-9_]*_20\d{6}"),
        re.compile(r"(?<!v_)final_[A-Za-z][A-Za-z0-9_]*_20\d{6}"),
    ]

    for prog_name, command in module._iter_click_commands(cli, "doctrail"):
        help_text = module._format_click_help(command, prog_name)
        for forbidden in forbidden_literals:
            assert forbidden not in help_text, f"{prog_name} help contains stale name {forbidden}"
        for pattern in forbidden_patterns:
            assert not pattern.search(help_text), f"{prog_name} help contains stale pattern {pattern.pattern}"


def test_llms_full_is_fresh():
    module = _load_build_llms_module()
    config = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=module._MkdocsLoader)
    manual_paths = list(module.iter_manual_paths(config.get("nav", [])))
    parts = [
        "# Doctrail full manual",
        "",
        "Generated from mkdocs navigation by `scripts/build_llms_full.py`.",
        "",
    ]
    for rel_path in manual_paths:
        if rel_path == module.CLI_DOC:
            text = module.render_cli_reference()
        else:
            text = (DOCS / rel_path).read_text(encoding="utf-8").strip()
        parts.extend([f"<!-- {rel_path} -->", "", text, ""])

    expected = "\n".join(parts).rstrip() + "\n"
    actual = (DOCS / "llms.txt").read_text(encoding="utf-8")
    assert actual == expected, "docs/llms.txt is stale; run scripts/build_llms_full.py"
