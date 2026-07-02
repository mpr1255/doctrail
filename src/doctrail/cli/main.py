"""
Main CLI group and project initialization commands.
"""
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import asyncio
import json
import sqlite3
from importlib import resources
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import click
import yaml

from ..core_utils import load_doctrail_environment, parse_env_file
from .utils import (
    CONTEXT_SETTINGS, __version__,
    PRESET_ENRICHMENTS, PRESET_ALIASES,
    _load_project_config, _get_doctrail_dir, _get_config_path,
    _resolve_preset_alias, _get_presets_dir, _load_presets,
    _exit_error, get_db_connection
)
from ..db_operations import (
    ENRICHMENT_RUNS_TABLE,
    ENRICHMENTS_TABLE,
    _doctrail_view_name,
)


def _normalize_docs_input(path: str) -> str:
    """Trim a user-supplied docs path without mangling parent-dir refs.

    `.strip('./')` used to remove any mix of '.' and '/', which turned
    '../data' into 'data'. This only removes a leading './' and a
    trailing '/'.
    """
    path = path.strip()
    if path.startswith("./"):
        path = path[2:]
    return path.rstrip("/")


def _can_use_questionary() -> bool:
    """Return whether rich interactive prompts are safe in this process."""
    return sys.stdin.isatty()


def _ensure_wizard_tty(
    guidance: str = "Pass -p/--prompt (plus -o/--output, --type, or --enum as needed)."
) -> None:
    """Fail clearly before interactive prompts run without a terminal."""
    if not sys.stdin.isatty():
        raise click.UsageError(f"Interactive wizard requires a terminal. {guidance}")


def _resolve_docs_path(cwd: Path, docs_path: str) -> Path:
    """Resolve a configured docs path relative to the project folder."""
    path = Path(os.path.expanduser(docs_path))
    return path if path.is_absolute() else cwd / path


def _format_docs_path_for_config(docs_path: str) -> str:
    """Format a docs path for config.yml without breaking absolute paths."""
    normalized = _normalize_docs_input(docs_path)
    if normalized in {"", "."}:
        return "./"
    if normalized.startswith("~") or Path(os.path.expanduser(normalized)).is_absolute():
        return f"{normalized}/"
    return f"./{normalized}/"


def _path_for_project_config(cwd: Path, selected_path: str) -> str:
    """Prefer relative paths inside the project and absolute paths outside it."""
    resolved = Path(os.path.expanduser(selected_path)).resolve()
    try:
        relative = resolved.relative_to(cwd.resolve())
        return str(relative) if str(relative) else "."
    except ValueError:
        return str(resolved)


def _count_documents(folder: Path, doc_extensions: set[str]) -> int:
    """Count supported document files in a folder."""
    if not folder.exists() or not folder.is_dir():
        return 0
    return sum(1 for path in folder.rglob("*") if path.suffix.lower() in doc_extensions)


def _choose_folder_with_system_dialog(cwd: Path) -> Optional[str]:
    """Open a native folder picker when the platform provides a simple one."""
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        return None

    script = 'POSIX path of (choose folder with prompt "Choose the folder containing your documents")'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    selected = result.stdout.strip()
    if not selected:
        return None
    return _path_for_project_config(cwd, selected)


def _prompt_for_docs_folder(cwd: Path, found_folder: Optional[str],
                            doc_extensions: set[str]) -> Optional[str]:
    """Choose the document folder during init."""
    default_path = found_folder or "data"

    if _can_use_questionary():
        try:
            import questionary

            choices = []
            if found_folder:
                found_count = _count_documents(cwd / found_folder, doc_extensions)
                choices.append(questionary.Choice(
                    f"Use ./{found_folder}/ ({found_count} files found)",
                    value="found",
                ))
            choices.extend([
                questionary.Choice("Browse with the system folder picker", value="browse"),
                questionary.Choice("Type or paste a folder path", value="type"),
                questionary.Choice("Create ./data/ for documents I will add later", value="data"),
                questionary.Choice("Skip document-folder setup", value="skip"),
            ])
            action = questionary.select(
                "Where are your documents?",
                choices=choices,
                default="found" if found_folder else "browse",
            ).ask()
            if action is None:
                raise click.Abort()
            if action == "found":
                return found_folder
            if action == "skip":
                return None
            if action == "data":
                return "data"
            if action == "browse":
                selected = _choose_folder_with_system_dialog(cwd)
                if selected:
                    return selected
                click.echo("No system folder picker available. Falling back to path entry.")

            selected_path = questionary.path(
                "Folder path",
                default=f"./{default_path}/",
                only_directories=True,
            ).ask()
            if selected_path is None:
                raise click.Abort()
            return _normalize_docs_input(_path_for_project_config(cwd, selected_path))
        except ImportError:
            pass

    click.echo("Tip: you can drag a folder from Finder/File Explorer into this terminal to paste its path.")
    return _normalize_docs_input(click.prompt(
        "Where are your documents?",
        default=f"./{default_path}/",
    ))


def _safe_write(path: Path, content: str) -> str:
    """Write content, preserving any prior version whose content differs.

    Returns 'created', 'unchanged', or 'updated'. On 'updated', the prior
    contents are saved to `<name>.<timestamp>.bak` next to the file so
    reinit never silently destroys user edits.
    """
    if path.exists():
        existing = path.read_text()
        if existing == content:
            return 'unchanged'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
        backup_path.write_text(existing)
        path.write_text(content)
        return 'updated'
    path.write_text(content)
    return 'created'


def _env_line_key(raw_line: str) -> Optional[str]:
    """Return the key assigned by a .env line, ignoring comments and blank lines."""
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, separator, _value = line.partition("=")
    if not separator:
        return None
    key = key.strip()
    return key or None


def _upsert_env_value(env_path: Path, key: str, value: str) -> None:
    """Set one .env key without discarding unrelated local settings."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    updated = False

    for index, raw_line in enumerate(lines):
        if _env_line_key(raw_line) != key:
            continue
        prefix = "export " if raw_line.lstrip().startswith("export ") else ""
        lines[index] = f"{prefix}{key}={value}"
        updated = True

    if not updated:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")


def _ensure_gitignore_patterns(gitignore_path: Path, patterns: Sequence[str]) -> None:
    """Ensure each requested .gitignore pattern is present as its own entry."""
    lines = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
    existing_patterns = {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing_patterns = [pattern for pattern in patterns if pattern not in existing_patterns]
    if not missing_patterns:
        return

    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(missing_patterns)
    gitignore_path.write_text("\n".join(lines) + "\n")


def _slugify_enrichment_name(name: str) -> str:
    """Convert a user-facing enrichment name into a safe YAML filename/name."""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower()).strip("_")
    if not slug:
        raise click.UsageError("Enrichment name cannot be empty.")
    return slug


def _quote_sql_identifier(identifier: str) -> str:
    """Quote a SQLite identifier for wizard-generated SQL."""
    if not identifier or "\x00" in identifier:
        raise click.UsageError("Invalid SQL identifier.")
    return '"' + identifier.replace('"', '""') + '"'


def _quote_sql_literal(value: Any) -> str:
    """Quote a SQLite literal for wizard-generated SQL."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _project_db_path(config_data: Dict[str, Any]) -> Optional[Path]:
    """Return the configured project database path, resolved relative to cwd."""
    db_path = config_data.get("database")
    if not db_path:
        return None
    path = Path(os.path.expanduser(str(db_path)))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _get_source_columns(config_data: Dict[str, Any]) -> List[str]:
    """Inspect the configured source table and return column names if available."""
    db_path = _project_db_path(config_data)
    table_name = config_data.get("default_table", "documents")
    if not db_path or not db_path.exists() or not table_name:
        return []

    try:
        with get_db_connection(str(db_path)) as conn:
            table_ref = _quote_sql_identifier(str(table_name))
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table_ref})").fetchall()]
    except Exception:
        return []


def _default_input_columns(columns: Sequence[str]) -> List[str]:
    """Choose reasonable prompt payload columns from a source table."""
    if not columns:
        return ["raw_content:3000"]

    selected: List[str] = []
    for preferred in ("title", "filename", "abstract"):
        if preferred in columns:
            selected.append(preferred)

    for preferred in ("raw_content", "content", "full_text", "body", "text"):
        if preferred in columns:
            selected.append(f"{preferred}:3000")
            break

    if selected:
        return selected

    excluded = {"rowid", "sha1", "id"}
    fallback = [col for col in columns if col not in excluded]
    return fallback[:2] or ["raw_content:3000"]


def _input_column_choices(columns: Sequence[str]) -> List[Any]:
    """Build questionary checkbox choices for source input columns."""
    defaults = set(_default_input_columns(columns))
    choices: List[Any] = []
    for column in columns:
        if column in {"rowid", "sha1", "id"}:
            continue
        value = f"{column}:3000" if column in {"raw_content", "content", "full_text", "body", "text"} else column
        title = value
        choices.append((title, value, value in defaults))
    return choices


def _prompt_for_input_columns(config_data: Dict[str, Any]) -> List[str]:
    """Ask which source columns should be sent to the model."""
    _ensure_wizard_tty()
    columns = _get_source_columns(config_data)
    defaults = _default_input_columns(columns)
    default_text = ", ".join(defaults)

    if columns:
        try:
            import questionary

            choices = [
                questionary.Choice(title=title, value=value, checked=checked)
                for title, value, checked in _input_column_choices(columns)
            ]
            if choices:
                selected = questionary.checkbox(
                    "Which columns should be sent to the model?",
                    choices=choices,
                    instruction="Use :N to truncate large text fields after editing YAML.",
                ).ask()
                if selected:
                    return list(selected)
        except ImportError:
            pass

        click.echo("Available source columns: " + ", ".join(columns))

    raw_columns = click.prompt(
        "Input columns sent to the model",
        default=default_text,
    )
    return [col.strip() for col in raw_columns.split(",") if col.strip()]


def _prompt_for_scope_query(config_data: Dict[str, Any]) -> str:
    """Ask which rows an enrichment applies to and return a key-only query."""
    _ensure_wizard_tty()
    table_name = str(config_data.get("default_table", "documents"))
    key_column = str(config_data.get("key_column", "sha1"))
    table_ref = _quote_sql_identifier(table_name)
    key_ref = _quote_sql_identifier(key_column)
    default_query = f"SELECT rowid, {key_ref} FROM {table_ref} ORDER BY rowid"
    columns = _get_source_columns(config_data)

    mode = "all"
    try:
        import questionary

        selected = questionary.select(
            "Which rows should this enrichment apply to?",
            choices=[
                questionary.Choice("All rows in the source table", value="all"),
                questionary.Choice("Rows matching one simple filter", value="filter"),
                questionary.Choice("Advanced SQL", value="sql"),
            ],
            default="all",
        ).ask()
        if selected is None:
            raise click.Abort()
        mode = selected
    except ImportError:
        click.echo("Scope options: all, filter, sql")
        mode = click.prompt("Scope", default="all", type=click.Choice(["all", "filter", "sql"]))

    if mode == "all":
        return "all_docs" if config_data.get("sql_queries", {}).get("all_docs") else default_query

    if mode == "sql":
        edited = click.edit(default_query)
        return (edited or default_query).strip()

    if columns:
        try:
            import questionary

            column = questionary.select("Filter column", choices=list(columns)).ask()
            if column is None:
                raise click.Abort()
        except ImportError:
            click.echo("Available source columns: " + ", ".join(columns))
            column = click.prompt("Filter column")
    else:
        column = click.prompt("Filter column")

    operators = {
        "equals": "=",
        "not equals": "!=",
        "contains": "LIKE",
        "is not empty": "IS NOT EMPTY",
        "is empty": "IS EMPTY",
        "greater than": ">",
        "less than": "<",
        "at least": ">=",
        "at most": "<=",
    }
    try:
        import questionary

        op_label = questionary.select("Filter operator", choices=list(operators.keys())).ask()
        if op_label is None:
            raise click.Abort()
    except ImportError:
        op_label = click.prompt("Filter operator", default="contains", type=click.Choice(list(operators.keys())))

    column_ref = _quote_sql_identifier(str(column))
    operator = operators[op_label]
    if operator == "IS NOT EMPTY":
        predicate = f"{column_ref} IS NOT NULL AND {column_ref} != ''"
    elif operator == "IS EMPTY":
        predicate = f"{column_ref} IS NULL OR {column_ref} = ''"
    else:
        value = click.prompt("Filter value")
        if operator == "LIKE":
            value = f"%{value}%"
        predicate = f"{column_ref} {operator} {_quote_sql_literal(value)}"

    return (
        f"SELECT rowid, {key_ref} FROM {table_ref}\n"
        f"WHERE {predicate}\n"
        "ORDER BY rowid"
    )


def _default_scope_query(config_data: Dict[str, Any]) -> str:
    """Return the non-interactive key-only scope query for generated enrichments."""
    all_docs = config_data.get("sql_queries", {}).get("all_docs")
    if all_docs:
        return "all_docs"

    table_name = str(config_data.get("default_table", "documents"))
    key_column = str(config_data.get("key_column", "sha1"))
    table_ref = _quote_sql_identifier(table_name)
    key_ref = _quote_sql_identifier(key_column)
    return f"SELECT rowid, {key_ref} FROM {table_ref} ORDER BY rowid"


def _schema_from_type(output_type: str, enum_values: Optional[str] = None) -> Any:
    """Convert CLI/wizard output choices into a doctrail schema fragment."""
    if enum_values:
        values = [value.strip() for value in enum_values.split(",") if value.strip()]
        if not values:
            raise click.UsageError("--enum must include at least one value.")
        return {"enum": values}
    if output_type == "boolean":
        return "boolean"
    if output_type == "integer":
        return "integer"
    if output_type == "number":
        return "number"
    if output_type == "array":
        return {"type": "array", "items": {"type": "string"}}
    return "string"


def _prompt_for_schema(safe_name: str, output: Optional[str], output_type: str,
                       enum_values: Optional[str]) -> tuple[Any, Optional[str]]:
    """Ask for the output shape and return (schema, output_column)."""
    _ensure_wizard_tty()
    if enum_values or output_type != "string":
        output_column = output or click.prompt("Output column name", default=safe_name)
        return _schema_from_type(output_type, enum_values), output_column

    try:
        import questionary

        shape = questionary.select(
            "What shape should the answer have?",
            choices=[
                questionary.Choice("Label: pick one from a list", value="label"),
                questionary.Choice("Multiple labels: pick any from a list", value="multi_label"),
                questionary.Choice("Yes/no", value="boolean"),
                questionary.Choice("Number", value="number"),
                questionary.Choice("Short text", value="string"),
                questionary.Choice("Several fields", value="multi_field"),
            ],
            default="string",
        ).ask()
        if shape is None:
            raise click.Abort()
    except ImportError:
        shape = click.prompt(
            "Answer shape",
            default="string",
            type=click.Choice(["label", "multi_label", "boolean", "number", "string", "multi_field"]),
        )

    if shape == "multi_field":
        raw_fields = click.prompt(
            "Fields as name:type pairs",
            default="field_1:string",
        )
        schema: Dict[str, Any] = {}
        for raw_field in raw_fields.split(","):
            name_part, _, type_part = raw_field.strip().partition(":")
            field_name = _slugify_enrichment_name(name_part)
            field_type = type_part.strip() or "string"
            if field_type not in {"string", "integer", "number", "boolean"}:
                raise click.UsageError(f"Unsupported field type for {field_name}: {field_type}")
            schema[field_name] = {"type": field_type}
        return schema, None

    output_column = output or click.prompt("Output column name", default=safe_name)
    if shape == "label":
        values = click.prompt("Allowed labels, comma-separated")
        return _schema_from_type("string", values), output_column
    if shape == "multi_label":
        values = [value.strip() for value in click.prompt("Allowed labels, comma-separated").split(",") if value.strip()]
        if not values:
            raise click.UsageError("Multiple-label enrichments need at least one label.")
        return {"enum_list": values}, output_column
    if shape == "boolean":
        return "boolean", output_column
    if shape == "number":
        return "number", output_column
    return "string", output_column


def _write_enrichment_yaml(enrichment_config: Dict[str, Any], *, overwrite: bool = False) -> Path:
    """Write one enrichment YAML into .doctrail/enrichments."""
    enrichments_dir = _get_doctrail_dir() / "enrichments"
    enrichments_dir.mkdir(parents=True, exist_ok=True)

    safe_name = enrichment_config["name"]
    file_path = enrichments_dir / f"{safe_name}.yml"
    if file_path.exists() and not overwrite:
        if not click.confirm(f"'{file_path}' exists. Overwrite?"):
            raise click.Abort()

    file_path.write_text(_render_enrichment_template(enrichment_config))
    return file_path


class _TemplateYamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _dump_yaml_fragment(value: Any, *, indent: int = 0) -> str:
    """Dump a YAML value for embedding inside a commented template."""
    rendered = yaml.dump(
        value,
        Dumper=_TemplateYamlDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).rstrip()
    if rendered.endswith("\n..."):
        rendered = rendered[:-4].rstrip()
    if indent:
        rendered = textwrap.indent(rendered, " " * indent)
    return rendered


def _render_block_scalar(value: str, *, indent: int = 2) -> str:
    lines = str(value).rstrip("\n").splitlines() or [""]
    return "\n".join((" " * indent) + line for line in lines)


def _render_enrichment_template(enrichment_config: Dict[str, Any]) -> str:
    """Render a user-facing enrichment template with short teaching comments."""
    input_config = enrichment_config.get("input", {})
    parts = [
        "# Starter enrichment generated by doctrail new.",
        "# Prompt checklist:",
        "# - Write the prompt as a codebook: define enum values, anchor scale points, and state gate/null behavior.",
        "# - Keep the prompt static. Put row-specific text in input_columns, using :N to truncate long fields.",
        "# - Avoid {column} placeholders: a row-specific token breaks the shared prompt prefix there,",
        "#   so everything after it re-bills at full input price per row. If one is truly needed,",
        "#   put it at the very end.",
        "# Run `doctrail docs` for the full YAML guide.",
        f"name: {enrichment_config['name']}",
        f"description: {_dump_yaml_fragment(enrichment_config.get('description', ''))}",
        "input:",
        f"  query: {_dump_yaml_fragment(input_config.get('query', 'all_docs'))}",
        "  # Per-row content goes here; it is appended after the static prompt.",
        "  input_columns:",
        _dump_yaml_fragment(input_config.get("input_columns", ["raw_content:3000"]), indent=4),
        "prompt: |",
        _render_block_scalar(str(enrichment_config.get("prompt", "")), indent=2),
        "schema:",
        _dump_yaml_fragment(enrichment_config.get("schema", {}), indent=2),
    ]
    if enrichment_config.get("output_column"):
        parts.append(f"output_column: {_dump_yaml_fragment(enrichment_config['output_column'])}")
    return "\n".join(parts) + "\n"


def create_enrichment_interactively(
    *,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    output: Optional[str] = None,
    output_type: str = "string",
    enum_values: Optional[str] = None,
    overwrite: bool = False,
) -> tuple[str, Path]:
    """Create an enrichment YAML from CLI flags or the shared wizard."""
    try:
        config_data = _load_project_config()
    except click.UsageError:
        _exit_error("No doctrail project found. Run 'doctrail init' first.")

    flag_mode = prompt is not None

    if not name and flag_mode:
        raise click.UsageError("Enrichment name is required when using -p/--prompt.")
    if not name:
        _ensure_wizard_tty()
        name = click.prompt("Enrichment name", default="my_enrichment")
    safe_name = _slugify_enrichment_name(name)

    scope_query = _default_scope_query(config_data) if flag_mode else _prompt_for_scope_query(config_data)

    input_columns = _prompt_for_input_columns(config_data) if not flag_mode else _default_input_columns(_get_source_columns(config_data))

    if not prompt:
        _ensure_wizard_tty()
        click.echo("\nWrite the question or instruction for the model.")
        click.echo("Treat it like a codebook: define labels, anchor scales, and say when fields should be null.")
        click.echo("Keep row text in input_columns with :N limits; run `doctrail docs` for examples.")
        click.echo("For complex prompts, create the starter YAML now and then run: doctrail edit " + safe_name)
        prompt = click.prompt("Prompt")
    elif output is None:
        output = safe_name

    if flag_mode:
        schema = _schema_from_type(output_type, enum_values)
        output_column = output or safe_name
    else:
        schema, output_column = _prompt_for_schema(safe_name, output, output_type, enum_values)

    enrichment_config: Dict[str, Any] = {
        "name": safe_name,
        "description": str(prompt).splitlines()[0][:100],
        "input": {
            "query": scope_query,
            "input_columns": input_columns,
        },
        "prompt": str(prompt),
        "schema": schema,
    }
    if output_column:
        enrichment_config["output_column"] = output_column

    file_path = _write_enrichment_yaml(enrichment_config, overwrite=overwrite)
    return safe_name, file_path


def _prompt_init_run_action(enrichment_name: str) -> tuple[str, Optional[int]]:
    """Ask what to do with a newly available enrichment during init."""
    _ensure_wizard_tty()
    try:
        import questionary

        action = questionary.select(
            f"What do you want to do with '{enrichment_name}' now?",
            choices=[
                questionary.Choice("Preview only: doctrail enrich --dry-run", value="dry-run"),
                questionary.Choice("Pilot run on a small sample: --limit N", value="limit"),
                questionary.Choice("Run the full rowset", value="full"),
                questionary.Choice("Skip for now", value="skip"),
            ],
            default="dry-run",
        ).ask()
        if action is None:
            return "skip", None
    except ImportError:
        action = click.prompt(
            "Run now? Choose dry-run, limit, full, or skip",
            default="dry-run",
            type=click.Choice(["dry-run", "limit", "full", "skip"]),
        )

    if action == "limit":
        return action, click.prompt("How many rows?", default=5, type=int)
    return action, None


def _invoke_enrich_from_init(ctx: click.Context, enrichment_name: str, action: str,
                             limit: Optional[int]) -> None:
    """Run the enrich command from init using the normal CLI code path."""
    if action == "skip":
        return

    config_data = _load_project_config()
    db_path = _project_db_path(config_data)
    if not db_path or not db_path.exists():
        click.echo("\nDatabase does not exist yet. Run `doctrail ingest` before previewing or running enrichments.")
        return

    from .enrich import enrich as enrich_command

    click.echo("")
    ctx.invoke(
        enrich_command,
        enrichment_names=(enrichment_name,),
        config=None,
        enrichments=(),
        limit=limit if action == "limit" else None,
        overwrite=False,
        verbose=False,
        log_updates=False,
        model=None,
        db_path=None,
        output_db=None,
        batch_size=None,
        rowid=None,
        sha1=None,
        truncate=False,
        skip_cost_check=False,
        cost_threshold=5.0,
        where_clause=None,
        override_query=None,
        project=None,
        dry_run=action == "dry-run",
        dedupe_scope="query",
        materialize_inputs=True,
        execution_mode="sync",
        allow_column_collision=False,
    )


def _repo_root_from_source() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_packaged_doc(name: str) -> str:
    source_path = _repo_root_from_source() / "docs" / name
    if source_path.exists():
        return source_path.read_text(encoding="utf-8")

    try:
        return (
            resources.files("doctrail")
            .joinpath("_docs")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Packaged manual file '{name}' was not found. Regenerate docs/llms.txt and rebuild the package."
        ) from exc


def _read_packaged_skill() -> str:
    source_path = _repo_root_from_source() / "skills" / "doctrail" / "SKILL.md"
    if source_path.exists():
        return source_path.read_text(encoding="utf-8")

    try:
        return (
            resources.files("doctrail")
            .joinpath("_docs")
            .joinpath("SKILL.md")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            "Packaged Doctrail skill was not found. Rebuild the package with skills/doctrail/SKILL.md included."
        ) from exc


def _install_packaged_skill(force: bool) -> None:
    text = _read_packaged_skill()
    destination = Path.home() / ".claude" / "skills" / "doctrail" / "SKILL.md"

    if destination.exists():
        existing = destination.read_text(encoding="utf-8")
        if existing == text:
            click.echo(f"Skill already installed: {destination}")
            return
        if not force:
            raise click.ClickException(
                f"{destination} already exists and differs. Use --force to overwrite, "
                f"or compare with: doctrail skill > /tmp/doctrail.SKILL.md && "
                f"diff -u {destination} /tmp/doctrail.SKILL.md"
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    click.echo(f"Skill installed: {destination}")


def _tutorial_resource_root():
    source_path = _repo_root_from_source() / "examples" / "tutorial"
    if source_path.exists():
        return source_path
    return resources.files("doctrail").joinpath("_examples").joinpath("tutorial")


def _resource_as_path(resource):
    if isinstance(resource, Path):
        return nullcontext(resource)
    return resources.as_file(resource)


def _ensure_tutorial_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    columns = {
        "corpus": "TEXT",
        "paper_number": "INTEGER",
        "paper_group": "TEXT",
        "consensus_author": "TEXT",
        "country": "TEXT",
        "retrieved_on": "TEXT",
    }
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {sql_type}")


def _apply_tutorial_metadata(db_path: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with sqlite3.connect(db_path) as conn:
        _ensure_tutorial_columns(conn)
        for filename, metadata in manifest.items():
            conn.execute(
                """
                UPDATE documents
                SET corpus = ?,
                    paper_number = ?,
                    paper_group = ?,
                    consensus_author = ?,
                    country = ?,
                    retrieved_on = ?
                WHERE filename = ?
                """,
                (
                    metadata.get("corpus"),
                    metadata.get("paper_number"),
                    metadata.get("paper_group"),
                    metadata.get("consensus_author"),
                    metadata.get("country"),
                    metadata.get("retrieved_on"),
                    filename,
                ),
            )
        conn.commit()


def _copy_tutorial_doctrail(root: Path, destination: Path, corpus: Optional[str]) -> None:
    source = root / ".doctrail"
    if corpus is None:
        shutil.copytree(source, destination)
        return

    destination.mkdir()
    shutil.copy2(source / "config.yml", destination / "config.yml")

    if corpus == "fed":
        enrichment_names = ["test"]
    elif corpus == "econ-threat":
        enrichment_names = ["econ_threat"]
    else:
        enrichment_names = [
            "securitization",
            "country_mentions",
            "country_stance",
            "mentions_climate",
            "optimism",
        ]
    enrichments_dir = destination / "enrichments"
    replay_dir = destination / "replay"
    enrichments_dir.mkdir()
    replay_dir.mkdir()
    for name in enrichment_names:
        shutil.copy2(source / "enrichments" / f"{name}.yml", enrichments_dir / f"{name}.yml")
        shutil.copy2(source / "replay" / f"{name}.jsonl", replay_dir / f"{name}.jsonl")

    view_names = {"un": "country_mentions", "econ-threat": "econ_threat"}.get(corpus)
    if view_names:
        views_dir = destination / "views"
        views_dir.mkdir()
        shutil.copy2(source / "views" / f"{view_names}.yml", views_dir / f"{view_names}.yml")


def _init_test_scaffold(corpus: Optional[str] = None) -> None:
    cwd = Path.cwd()
    if (cwd / ".doctrail").exists():
        raise click.UsageError("Refusing to scaffold tutorial project: .doctrail already exists.")
    if (cwd / "data").exists():
        raise click.UsageError("Refusing to scaffold tutorial project: data/ already exists.")
    if (cwd / "out" / "database.db").exists():
        raise click.UsageError("Refusing to scaffold tutorial project: out/database.db already exists.")

    resource_root = _tutorial_resource_root()
    with _resource_as_path(resource_root) as root:
        if not (root / "corpus").exists() or not (root / ".doctrail").exists():
            raise click.ClickException("Tutorial scaffold resources are missing from the package.")

        corpus_root = root / "corpus"
        data_dir = cwd / "data"
        data_dir.mkdir()
        if corpus in (None, "fed"):
            federalist_dir = corpus_root / "federalist"
            for source_path in sorted(federalist_dir.iterdir()):
                if source_path.is_file():
                    shutil.copy2(source_path, data_dir / source_path.name)
        if corpus in (None, "un"):
            shutil.copytree(corpus_root / "un_speeches", data_dir / "un_speeches")
        if corpus == "econ-threat":
            for source_path in sorted((corpus_root / "gt_editorials").iterdir()):
                if source_path.is_file() and source_path.suffix == ".txt":
                    shutil.copy2(source_path, data_dir / source_path.name)
        shutil.copy2(corpus_root / "manifest.json", data_dir / "manifest.json")
        _copy_tutorial_doctrail(root, cwd / ".doctrail", corpus)

    out_dir = cwd / "out"
    out_dir.mkdir(exist_ok=True)
    db_path = out_dir / "database.db"

    from ..core import run_ingest

    asyncio.run(run_ingest(
        db_path=str(db_path),
        input_dirs=[str(cwd / "data")],
        table="documents",
        yes=True,
        workers=1,
        overwrite=True,
        skip_garbage_check=True,
        exclude_pattern="manifest.json,federalist_consensus.csv",
        pdf_engine="pymupdf",
        html_extractor="default",
    ))
    _apply_tutorial_metadata(db_path, cwd / "data" / "manifest.json")

    with sqlite3.connect(db_path) as conn:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    if corpus is None:
        click.echo(
            "\n"
            "Tutorial project initialized.\n\n"
            "   .doctrail/config.yml\n"
            "   .doctrail/enrichments/test.yml\n"
            "   .doctrail/replay/*.jsonl\n"
            "   data/ tutorial corpus\n"
            "   out/database.db\n\n"
            f"Documents ingested: {doc_count}\n\n"
            "Next step:\n"
            "   doctrail run test\n"
        )
    elif corpus == "fed":
        click.echo(
            "\n"
            "Tutorial project initialized.\n\n"
            "   .doctrail/config.yml\n"
            "   .doctrail/enrichments/test.yml\n"
            "   .doctrail/replay/test.jsonl\n"
            "   data/ Federalist tutorial corpus\n"
            "   out/database.db\n\n"
            f"Documents ingested: {doc_count}\n\n"
            "Next step:\n"
            "   doctrail run test\n"
        )
    elif corpus == "econ-threat":
        click.echo(
            "\n"
            "Tutorial project initialized.\n\n"
            "   .doctrail/config.yml\n"
            "   .doctrail/enrichments/econ_threat.yml\n"
            "   .doctrail/replay/econ_threat.jsonl\n"
            "   .doctrail/views/econ_threat.yml\n"
            "   data/ Global Times editorials (Chinese)\n"
            "   out/database.db\n\n"
            f"Documents ingested: {doc_count}\n\n"
            "Next steps:\n"
            "   doctrail run econ_threat\n"
            "   doctrail view spec econ_threat\n"
        )
    else:
        click.echo(
            "\n"
            "Tutorial project initialized.\n\n"
            "   .doctrail/config.yml\n"
            "   .doctrail/enrichments/{securitization,country_mentions,country_stance,mentions_climate,optimism}.yml\n"
            "   .doctrail/replay/{securitization,country_mentions,country_stance,mentions_climate,optimism}.jsonl\n"
            "   .doctrail/views/country_mentions.yml\n"
            "   data/ UN speeches tutorial corpus\n"
            "   out/database.db\n\n"
            f"Documents ingested: {doc_count}\n\n"
            "Next step:\n"
            "   doctrail run securitization --limit 100\n"
        )

# Create the main CLI group
@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option('--skip-requirements', is_flag=True, help='Skip system requirements check')
@click.option('--version', '-v', is_flag=True, help='Show version')
@click.pass_context
def cli(ctx, skip_requirements, version):
    """SQLite document enrichment with normalized outputs and derived views.

    \b
    Agents: run `doctrail agent` for the full operating manual in one shot —
    the mental model, the enrichment workflow, and troubleshooting. Start
    there; everything else is discoverable from it.

    Humans: `doctrail docs` prints the complete reference manual; this --help
    lists every command.
    """
    load_doctrail_environment()

    if version:
        click.echo(f"doctrail {__version__}")
        ctx.exit(0)

    ctx.ensure_object(dict)
    ctx.obj['skip_requirements'] = skip_requirements

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


@cli.command("docs")
def docs_command():
    """Print the packaged manual: everything an agent needs, in one file."""
    click.echo(_read_packaged_doc("llms.txt"), nl=False)


@cli.command("skill")
@click.option("--install", is_flag=True, help="Install the packaged Doctrail skill into ~/.claude/skills/doctrail/SKILL.md")
@click.option("--force", is_flag=True, help="Overwrite an existing installed skill when used with --install")
def skill_command(install: bool, force: bool):
    """Print or install the packaged Doctrail skill."""
    if install:
        _install_packaged_skill(force)
        return
    if force:
        raise click.UsageError("--force can only be used with --install.")
    click.echo(_read_packaged_skill(), nl=False)


@cli.command("agent")
def agent_command():
    """Print the full agent guide: mental model, workflow, troubleshooting.

    This is the entry point for an LLM or coding agent driving doctrail. It
    prints the complete operating manual to stdout, no install required. Same
    content as `doctrail skill`; `agent` is the name agents reach for.
    """
    click.echo(_read_packaged_skill(), nl=False)


@cli.command()
@click.argument('preset', required=False, type=click.Choice(['test']))
@click.argument('corpus', required=False, type=click.Choice(['fed', 'un', 'econ-threat']))
@click.option('--name', help='Project name (used for database filename)')
@click.option('--api-key', help='API key (or set interactively)')
@click.option('--provider', type=click.Choice(['openai', 'gemini', 'anthropic', 'openrouter']), default='openai',
              help='LLM provider (default: openai)')
@click.option('--docs', 'docs_path', help='Path to documents folder (relative to current dir)')
@click.option('--database', 'database_path', help='Path to SQLite database to use in config')
@click.option('--no-docs', is_flag=True, help='Skip document-folder setup for query-first projects')
@click.option('--no-env', is_flag=True, help='Do not create a .env file; rely on existing environment variables')
@click.option('--yes', '-y', is_flag=True, help='Skip prompts, use defaults')
@click.option('--enrichments', '-e', multiple=True, help='Enrichments to set up (can repeat)')
@click.pass_context
def init(
    ctx,
    preset: Optional[str],
    corpus: Optional[str],
    name: Optional[str],
    api_key: str,
    provider: str,
    docs_path: Optional[str],
    database_path: Optional[str],
    no_docs: bool,
    no_env: bool,
    yes: bool,
    enrichments: tuple,
):
    """
    Initialize a doctrail project in the current directory.

    Creates:
    - .doctrail/config.yml     (project settings)
    - .doctrail/enrichments/   (your analysis tasks)
    - out/{name}.db            (database, unless --database is used)
    - .env                     (API key, unless --no-env is used)

    Doctrail stores model outputs in normalized enrichment tables and then
    materializes user-facing views for review and analysis.

    Example:
        cd my_research/
        doctrail init
    """
    # Import ingest here to avoid circular imports
    from .ingest import ingest

    if corpus and preset != "test":
        raise click.UsageError("The corpus argument is only valid with 'doctrail init test'.")

    if preset == "test":
        _init_test_scaffold(corpus)
        return

    provider_settings = {
        "openai": {
            "model": "gpt-4o-mini",
            "env_vars": ["OPENAI_API_KEY"],
            "write_env_var": "OPENAI_API_KEY",
        },
        "gemini": {
            "model": "gemini-1.5-flash",
            "env_vars": ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_AI_API_KEY"],
            "write_env_var": "GOOGLE_API_KEY",
        },
        "anthropic": {
            "model": "claude-3-5-haiku-latest",
            "env_vars": ["ANTHROPIC_API_KEY"],
            "write_env_var": "ANTHROPIC_API_KEY",
        },
        "openrouter": {
            "model": "openrouter/openai/gpt-4o-mini",
            "env_vars": ["OPENROUTER_API_KEY"],
            "write_env_var": "OPENROUTER_API_KEY",
        },
    }

    click.echo(f"doctrail {__version__} — {len(PRESET_ENRICHMENTS)} presets available")
    click.echo("")

    cwd = Path.cwd()
    doctrail_dir = cwd / ".doctrail"
    enrichments_dir = doctrail_dir / "enrichments"
    doc_extensions = {'.pdf', '.doc', '.docx', '.html', '.htm', '.txt', '.epub', '.mhtml', '.mht'}

    if doctrail_dir.exists() and not yes:
        if not click.confirm("This folder is already initialized. Reinitialize?"):
            click.echo("Aborted.")
            return
    elif doctrail_dir.exists() and yes:
        click.echo("This folder is already initialized. Reinitializing with --yes.")

    # Step 1: Project name
    if not name:
        default_name = cwd.name.replace(' ', '_').lower()
        if yes:
            name = default_name
        else:
            name = click.prompt("Project name", default=default_name)
    name = name.replace(' ', '_').lower()
    click.echo(f"Project: {name}")

    # Step 2: Documents path
    doc_count = 0
    if no_docs:
        docs_path = None
        click.echo("Documents: not configured (--no-docs)")
    else:
        if not docs_path:
            common_folders = ['data', 'docs', 'papers', 'documents', 'pdfs', 'files']
            found_folder = None

            for folder in common_folders:
                folder_path = cwd / folder
                if folder_path.is_dir():
                    doc_count = _count_documents(folder_path, doc_extensions)
                    if doc_count > 0:
                        found_folder = folder
                        break

            if yes:
                docs_path = found_folder or 'data'
            else:
                docs_path = _prompt_for_docs_folder(cwd, found_folder, doc_extensions)

        if docs_path is None:
            click.echo("Documents: not configured")
        else:
            docs_path = _normalize_docs_input(docs_path)
            docs_full_path = _resolve_docs_path(cwd, docs_path)

            if docs_full_path.exists():
                doc_count = _count_documents(docs_full_path, doc_extensions)
                display_path = _format_docs_path_for_config(docs_path)
                click.echo(f"Documents: {display_path} ({doc_count} files found)")
            else:
                docs_full_path.mkdir(parents=True, exist_ok=True)
                display_path = _format_docs_path_for_config(docs_path)
                click.echo(f"Documents: {display_path} (folder created)")


    # Step 2b: Database path
    default_db = f"./out/{name}.db"
    if database_path:
        db_path = database_path
    elif yes:
        db_path = default_db
    else:
        db_path = click.prompt("Database path", default=default_db)
    click.echo(f"Database: {db_path}")

    # Step 3: API key
    env_file_path = cwd / ".env"
    api_key_source = None

    # When --provider is explicit, restrict auto-detection to that provider's
    # env vars so we don't silently swap the user's choice.
    provider_source = ctx.get_parameter_source('provider')
    provider_is_explicit = provider_source == click.core.ParameterSource.COMMANDLINE
    if provider_is_explicit:
        search_providers = {provider: provider_settings[provider]}
    else:
        search_providers = provider_settings

    if api_key:
        api_key_source = "cli"
    else:
        env_values = parse_env_file(env_file_path) if env_file_path.exists() else {}
        for provider_name, settings in search_providers.items():
            for candidate_env_var in settings["env_vars"]:
                if env_values.get(candidate_env_var):
                    api_key = env_values[candidate_env_var]
                    provider = provider_name
                    api_key_source = ".env file"
                    break
            if api_key_source:
                break
        if not api_key_source:
            for provider_name, settings in search_providers.items():
                for candidate_env_var in settings["env_vars"]:
                    if os.environ.get(candidate_env_var):
                        api_key = os.environ.get(candidate_env_var)
                        provider = provider_name
                        api_key_source = "environment"
                        break
                if api_key_source:
                    break

    if api_key and api_key_source:
        if yes:
            click.echo(f"✓ Using API key from {api_key_source} ({provider})")
        elif api_key_source != "cli":
            if not click.confirm(f"Found API key in {api_key_source}. Use it?"):
                api_key = click.prompt("Enter your API key", hide_input=True)
                api_key_source = "user input"
    else:
        if yes:
            raise click.UsageError(
                "No API key found. Set OPENAI_API_KEY, GOOGLE_API_KEY, GEMINI_API_KEY, GOOGLE_AI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, or use --api-key"
            )
        api_key = click.prompt("Enter your API key", hide_input=True)
        api_key_source = "user input"

    default_model = provider_settings[provider]["model"]
    env_var = provider_settings[provider]["write_env_var"]

    # Handle enrichments selection
    if enrichments:
        selected = [e for e in enrichments if e in PRESET_ENRICHMENTS]
        if not selected:
            raise click.UsageError(f"Unknown enrichments. Available: {', '.join(PRESET_ENRICHMENTS.keys())}")
    elif yes:
        if no_docs:
            selected = []
            click.echo("✓ Query-first project: no default enrichment scaffold created")
        else:
            selected = ["language"]
            click.echo("✓ Using default enrichment: language")
    else:
        if _can_use_questionary():
            try:
                import questionary
                from questionary import Style

                custom_style = Style([
                    ('qmark', 'fg:yellow bold'),
                    ('question', 'bold'),
                    ('pointer', 'fg:cyan bold'),
                    ('highlighted', 'fg:cyan bold'),
                    ('selected', 'fg:green'),
                ])

                choices = [
                    questionary.Choice(
                        title=f"{preset_name}: {info['description']}",
                        value=preset_name,
                        checked=(preset_name == "language")
                    )
                    for preset_name, info in PRESET_ENRICHMENTS.items()
                ]

                selected = questionary.checkbox(
                    "Select enrichments:",
                    choices=choices,
                    style=custom_style,
                    instruction="(↑↓ navigate, space select, enter confirm)",
                ).ask()

                if selected is None:
                    click.echo("Aborted.")
                    return

            except ImportError:
                selected = None
        else:
            selected = None

        if selected is None:
            click.echo("\nAvailable enrichments:\n")
            for i, (ename, info) in enumerate(PRESET_ENRICHMENTS.items(), 1):
                click.echo(f"  {i}. {ename}: {info['description']}")

            click.echo("\nEnter numbers separated by commas (e.g., 1,2,3)")
            click.echo("Or press Enter for default (language detection)")
            selection = click.prompt("Selection", default="", show_default=False)

            if selection.strip() == "":
                selected = ["language"]
            else:
                names = list(PRESET_ENRICHMENTS.keys())
                selected = []
                invalid_entries = []
                for raw_entry in selection.split(","):
                    entry = raw_entry.strip()
                    if not entry:
                        continue
                    if not entry.isdigit():
                        invalid_entries.append(entry)
                        continue
                    index = int(entry) - 1
                    if 0 <= index < len(names):
                        selected.append(names[index])
                    else:
                        invalid_entries.append(entry)
                if invalid_entries:
                    raise click.UsageError(
                        f"Invalid enrichment selection: {', '.join(invalid_entries)}. "
                        f"Choose numbers from 1 to {len(names)}."
                    )
    if not selected and not no_docs:
        selected = ["language"]

    # Create directory structure
    doctrail_dir.mkdir(exist_ok=True)
    enrichments_dir.mkdir(exist_ok=True)

    db_parent = Path(db_path).parent
    if db_parent != Path('.'):
        (cwd / db_parent).mkdir(parents=True, exist_ok=True)

    # Create main config.yml
    if docs_path:
        docs_config_path = _format_docs_path_for_config(docs_path)
        config_content = textwrap.dedent(f"""\
            # Doctrail project configuration: {name}
            # Generated by: doctrail init

            project_name: {name}
            database: {db_path}
            documents_path: {docs_config_path}
            default_model: {default_model}
            default_table: documents

            sql_queries:
              all_docs: |
                SELECT rowid, sha1 FROM documents
                ORDER BY rowid

            # Enrichments are loaded from .doctrail/enrichments/*.yml
            # Run them with: doctrail enrich <name>
        """)
    else:
        config_content = textwrap.dedent(f"""\
            # Doctrail project configuration: {name}
            # Generated by: doctrail init

            project_name: {name}
            database: {db_path}
            default_model: {default_model}
            sql_queries: {{}}

            # Query-first scaffold: set default_table and sql_queries for your source DB.
            # Enrichments are loaded from .doctrail/enrichments/*.yml
            # Run them with: doctrail enrich <name>
        """)

    backup_notes: list = []

    config_path = doctrail_dir / "config.yml"
    config_status = _safe_write(config_path, config_content)
    if config_status == 'updated':
        backup_notes.append(f"   Backed up previous .doctrail/config.yml → {config_path.name}.*.bak")

    for enrichment_name in selected:
        if enrichment_name in PRESET_ENRICHMENTS:
            preset = PRESET_ENRICHMENTS[enrichment_name]
            enrichment_path = enrichments_dir / preset["filename"]
            enrichment_status = _safe_write(enrichment_path, preset["content"])
            if enrichment_status == 'updated':
                backup_notes.append(
                    f"   Backed up previous .doctrail/enrichments/{preset['filename']} → {preset['filename']}.*.bak"
                )

    env_written = False
    if not no_env:
        env_path = cwd / ".env"
        _upsert_env_value(env_path, env_var, api_key)
        env_written = True

    gitignore_path = cwd / ".gitignore"
    _ensure_gitignore_patterns(gitignore_path, [".env", "*.db", "*.db-journal", ".doctrail/", "data/", "out/"])

    enrichment_list = ", ".join(selected)
    created_paths = ["   .doctrail/config.yml"]
    if selected:
        created_paths.append(f"   .doctrail/enrichments/{', '.join(f'{s}.yml' for s in selected)}")
    if env_written:
        created_paths.append("   .env (API key)")

    docs_label = _format_docs_path_for_config(docs_path) if docs_path else "not configured"
    next_steps = []
    if docs_path:
        next_steps.append("   doctrail ingest")
    if selected:
        next_steps.append(f"   doctrail enrich {selected[0]}")
        next_steps.append("   doctrail view        # inspect results")
    else:
        next_steps.append("   doctrail new         # create an enrichment interactively")

    backup_block = ("\n" + "\n".join(backup_notes) + "\n") if backup_notes else ""

    click.echo(
        "\n"
        f"Project '{name}' initialized.\n\n"
        + "\n".join(created_paths)
        + "\n\n"
        f"   Documents: {docs_label}\n"
        f"   Database:  {db_path}\n"
        f"   Enrichments: {enrichment_list or 'none'}\n"
        + backup_block
        + "\nNext steps:\n"
        + "\n".join(next_steps)
        + "\n"
    )

    if docs_path and doc_count > 0 and not yes:
        if click.confirm(f"Import {doc_count} documents now?", default=True):
            click.echo("")
            ctx.invoke(ingest, yes=True)

    created_custom_name = None
    if not yes:
        if click.confirm("Create a custom enrichment now?", default=False):
            created_custom_name, file_path = create_enrichment_interactively()
            click.echo(f"\nCreated: {file_path}")

        run_target = created_custom_name or (selected[0] if selected else None)
        if run_target:
            action, run_limit = _prompt_init_run_action(run_target)
            _invoke_enrich_from_init(ctx, run_target, action, run_limit)


@cli.command()
@click.argument('name', required=False)
@click.option('--prompt', '-p', help='Instructions for the LLM')
@click.option('--output', '-o', help='Output column/field name')
@click.option('--type', 'output_type', default='string',
              type=click.Choice(['string', 'integer', 'number', 'boolean', 'array']),
              help='Output type (default: string)')
@click.option('--enum', 'enum_values', help='Comma-separated enum values (e.g., "positive,negative,neutral")')
@click.option('--overwrite', is_flag=True, help='Overwrite an existing enrichment YAML')
def new(name: Optional[str], prompt: Optional[str], output: Optional[str],
        output_type: str, enum_values: Optional[str], overwrite: bool):
    """
    Create a new custom enrichment.

    Example:
        doctrail new sentiment --prompt "Classify the sentiment" --enum "positive,negative,neutral"
    """
    safe_name, file_path = create_enrichment_interactively(
        name=name,
        prompt=prompt,
        output=output,
        output_type=output_type,
        enum_values=enum_values,
        overwrite=overwrite,
    )
    click.echo(
        f"\nCreated: {file_path}\n\n"
        "Recommended workflow:\n"
        f"   doctrail enrich {safe_name} --dry-run\n"
        f"   doctrail enrich {safe_name} --limit 5\n"
        f"   doctrail edit {safe_name}\n"
        f"   doctrail enrich {safe_name}\n"
    )


@cli.command()
@click.argument('name')
def edit(name: str):
    """Open a project enrichment YAML in $EDITOR."""
    try:
        _load_project_config()
    except click.UsageError:
        _exit_error("No doctrail project found. Run 'doctrail init' first.")

    safe_name = _slugify_enrichment_name(_resolve_preset_alias(name))
    enrichments_dir = _get_doctrail_dir() / "enrichments"
    file_path = enrichments_dir / f"{safe_name}.yml"

    if not file_path.exists() and safe_name in PRESET_ENRICHMENTS:
        enrichments_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_text(PRESET_ENRICHMENTS[safe_name]["content"])
        click.echo(f"Copied preset '{safe_name}' to {file_path}")

    if not file_path.exists():
        available = sorted(path.stem for path in enrichments_dir.glob("*.yml")) if enrichments_dir.exists() else []
        raise click.UsageError(
            f"Enrichment '{name}' not found in {enrichments_dir}."
            + (f" Available: {', '.join(available)}" if available else "")
        )

    edited = click.edit(filename=str(file_path))
    if edited is None:
        click.echo(f"Edited: {file_path}")


@cli.command()
@click.argument('action', required=False, type=click.Choice(['list', 'new', 'refresh', 'create', 'pivot', 'spec', 'render']))
@click.argument('name', required=False)
@click.option('--table', 'source_table', help='Source table to join against (default: from config)')
@click.option('--run-id', help='Run ID to build a view for')
@click.option('-e', '--enrichment', help='Enrichment name (for pivot action)')
@click.option('--fields', help='Comma-separated field names to include (default: all)')
@click.option('--include', help='Source columns to include, with optional :N truncation (e.g. "title,raw_content:500")')
@click.option('--by-model', is_flag=True, help='Create per-model columns for ICR comparison')
@click.option('--output', help='Output path for render action')
@click.option('--format', 'render_format', type=click.Choice(['html', 'csv', 'json']), default='html',
              show_default=True, help='Output format for render action')
@click.option('--limit', 'render_limit', type=int, help='Optional row limit for render action')
def view(action: Optional[str], name: Optional[str], source_table: Optional[str],
         run_id: Optional[str], enrichment: Optional[str], fields: Optional[str],
         include: Optional[str], by_model: bool, output: Optional[str],
         render_format: str, render_limit: Optional[int]):
    """
    Manage derived views for reviewing and analyzing normalized enrichments.

    Doctrail stores model outputs in normalized tables (`_enrichments`,
    `_enrichment_audit`, `_enrichment_runs`). Views are the user-facing surface:
    they join source rows with selected enrichment fields in a wide format.

    \b
    Commands:
        doctrail view                     List all views in database
        doctrail view create              List recent runs / enrichments
        doctrail view create <enrichment> Materialize the latest run view for one enrichment
        doctrail view create --run-id <run_id>  Materialize one specific persisted run
        doctrail view new <name>          Create a custom view SQL file
        doctrail view spec <name|path>    Create/apply a YAML view spec
        doctrail view refresh             Execute all .doctrail/views/*.sql and *.yml
        doctrail view pivot <name> -e <enrichment>  Create a reusable wide analysis view
        doctrail view render <name>       Export a materialized view to HTML, CSV, or JSON

    \b
    View workflow:
        doctrail runs
        doctrail view create --run-id <run_id>
        doctrail overrides-export --run-id <run_id>

    \b
    Pivot examples:
        doctrail view pivot my_review -e framing_v6
        doctrail view pivot my_review -e framing --fields hostility,frame --include "title,raw_content:500"
        doctrail view pivot icr_check -e framing --by-model --include title

    \b
    View spec example:
        doctrail view spec payments_review
        doctrail view refresh
        doctrail view render payments_review --output payments_review.html

    \b
    Review shortcut:
        doctrail view create my_enrichment
        doctrail query "SELECT * FROM v_run_my_enrichment_20260228_1430 LIMIT 20"
    """
    try:
        config_data = _load_project_config()
    except click.UsageError:
        _exit_error("No doctrail project found. Run 'doctrail init' first.")

    db_path = config_data.get('database')
    if not db_path:
        _exit_error("No database configured.")

    views_dir = Path('.doctrail/views')

    if not action or action == 'list':
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
            views = cursor.fetchall()

        if not views:
            click.echo("No views in database yet.")
            click.echo("Run an enrichment to create the default view, or use 'doctrail view new <name>'")
            return

        click.echo(f"Views in database ({len(views)}):\n")
        for (view_name,) in views:
            with get_db_connection(db_path) as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"PRAGMA table_info({view_name})")
                    cols = len(cursor.fetchall())
                    click.echo(f"  {view_name} ({cols} columns)")
                except:
                    click.echo(f"  {view_name}")

        if views_dir.exists():
            sql_files = list(views_dir.glob("*.sql"))
            if sql_files:
                click.echo(f"\nCustom SQL files (.doctrail/views/):")
                for f in sql_files:
                    click.echo(f"  {f.name}")
            yaml_files = list(views_dir.glob("*.yml")) + list(views_dir.glob("*.yaml"))
            if yaml_files:
                click.echo(f"\n🧩 View specs (.doctrail/views/):")
                for f in sorted(yaml_files):
                    click.echo(f"  {f.name}")

        click.echo(f"\nQuery: doctrail query \"SELECT * FROM <view_name> LIMIT 10\"")

    elif action == 'new':
        if not name:
            _exit_error("Usage: doctrail view new <name>")

        views_dir.mkdir(parents=True, exist_ok=True)
        sql_file = views_dir / f"{name}.sql"

        if sql_file.exists():
            if not click.confirm(f"'{sql_file}' exists. Overwrite?"):
                return 0

        project_name = config_data.get('project_name', 'my_project')
        default_table = config_data.get('default_table', 'documents')

        key_col = config_data.get('key_column', 'sha1')
        template_view_name = _doctrail_view_name(name)
        template = f"""-- Custom view: {template_view_name}
-- After editing, run: doctrail view refresh
-- Source tables stay authoritative; views are the review surface.

DROP VIEW IF EXISTS {template_view_name};
CREATE VIEW {template_view_name} AS
SELECT
    d.{key_col},
    d.filename,
    (SELECT value FROM {ENRICHMENTS_TABLE} WHERE key_value = d.{key_col} AND field_name = 'summary'
     ORDER BY timestamp DESC LIMIT 1) as summary,
    (SELECT value FROM {ENRICHMENTS_TABLE} WHERE key_value = d.{key_col} AND field_name = 'language'
     ORDER BY timestamp DESC LIMIT 1) as language
FROM {default_table} d
ORDER BY d.filename;
"""
        sql_file.write_text(template)
        click.echo(f"Created: {sql_file}")
        click.echo(f"\nEdit the SQL, then run: doctrail view refresh")

    elif action == 'spec':
        def resolve_spec_path(spec_name: Optional[str]) -> Optional[Path]:
            if not spec_name:
                return None
            candidate = Path(spec_name).expanduser()
            if candidate.exists():
                return candidate
            if candidate.suffix.lower() in {'.yml', '.yaml'}:
                return candidate
            return views_dir / f"{spec_name}.yml"

        if not name:
            spec_files = sorted(list(views_dir.glob("*.yml")) + list(views_dir.glob("*.yaml"))) if views_dir.exists() else []
            if not spec_files:
                click.echo("No YAML view specs yet.")
                click.echo("Usage: doctrail view spec <name>")
                return 0
            click.echo("Available YAML view specs:\n")
            for spec_file in spec_files:
                click.echo(f"  {spec_file}")
            click.echo("\nApply one with: doctrail view spec <name>")
            return 0

        spec_path = resolve_spec_path(name)
        if spec_path is None:
            _exit_error("Usage: doctrail view spec <name>")

        if not spec_path.exists():
            views_dir.mkdir(parents=True, exist_ok=True)
            default_table = source_table or config_data.get('default_table', 'documents')
            key_col = config_data.get('key_column', 'sha1')
            spec_name = spec_path.stem
            template = f"""name: {spec_name}
enrichment: your_enrichment_name
run_id: latest
source_table: {default_table}
key_column: {key_col}
include:
  - title
  - raw_content:3000
columns:
  - field: english_translation
    enrichment: translate_to_english
    alias: english_translation
explode:
  field: items
  object_fields:
    - amount
    - payer
    - receiver
    - fund_name
    - evidence
  alias_prefix: item_
"""
            spec_path.write_text(template)
            click.echo(f"Created view spec template: {spec_path}")
            click.echo("Edit the enrichment, fields, and explode section, then run the same command again.")
            return 0

        from ..db_operations import create_view_from_spec

        try:
            spec_data = yaml.safe_load(spec_path.read_text()) or {}
            if not isinstance(spec_data, dict):
                raise ValueError("View spec must be a YAML mapping")
            spec_data.setdefault('name', spec_path.stem)
            if source_table and 'source_table' not in spec_data:
                spec_data['source_table'] = source_table

            result = create_view_from_spec(
                db_path=db_path,
                spec=spec_data,
                default_source_table=source_table or config_data.get('default_table', 'documents'),
                default_key_column=config_data.get('key_column', 'sha1'),
            )
        except Exception as e:
            _exit_error(f"Error: {e}")

        click.echo(f"Created view: {result['view_name']}")
        click.echo(f"  {result['row_count']} rows, {len(result['columns'])} columns")
        click.echo(f"  Anchored to: {result['anchored_to']}")
        if result.get('run_id'):
            click.echo(f"  Run ID: {result['run_id']}")
        click.echo(f"  Columns: {', '.join(result['columns'])}")
        click.echo(f"\nQuery it:")
        click.echo(f"  doctrail query \"SELECT * FROM {result['view_name']} LIMIT 20\"")

    elif action == 'create':
        if run_id:
            from ..db_operations import create_run_view

            docs_table = source_table or config_data.get('default_table', 'documents')
            key_col = config_data.get('key_column', 'sha1')
            view_name = create_run_view(
                db_path=db_path,
                run_id=run_id,
                documents_table=docs_table,
                key_column=key_col,
            )
            if not view_name:
                _exit_error(f"No enrichment fields found for run '{run_id[:8]}'; no view was created.")
            click.echo(f"Created view: {view_name}")
            click.echo(f"\nQuery it:")
            click.echo(f"  doctrail query \"SELECT * FROM {view_name} LIMIT 20\"")
            return 0

        if not name:
            # No enrichment name given — list what's available
            with get_db_connection(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (ENRICHMENT_RUNS_TABLE,),
                )
                has_runs_table = cursor.fetchone() is not None

            if has_runs_table:
                from ..db_operations import list_enrichment_runs
                recent_runs = list_enrichment_runs(db_path, limit=20)
            else:
                recent_runs = []

            if recent_runs:
                click.echo("Recent runs:\n")
                for row in recent_runs:
                    click.echo(
                        f"  {row['run_id'][:8]}  {row['enrichment_name']}  "
                        f"[{row['model']}]  {row['status']}"
                    )
                click.echo(f"\nUsage: doctrail view create --run-id <run_id>")
                return 0

            with get_db_connection(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT enrichment_name, prompt_hash, COUNT(DISTINCT key_value) as rows,
                           COUNT(DISTINCT field_name) as fields, MAX(timestamp) as last_run
                    FROM {ENRICHMENTS_TABLE}
                    GROUP BY enrichment_name, prompt_hash
                    ORDER BY last_run DESC
                """)
                runs = cursor.fetchall()

            if not runs:
                _exit_error("No enrichments in database yet.")

            click.echo("Available enrichments:\n")
            for ename, phash, row_count, field_count, ts in runs:
                click.echo(f"  {ename}  ({row_count} rows, {field_count} fields, prompt {phash[:8]})")
            click.echo(f"\nUsage: doctrail view create <enrichment_name>")
            return 0

        # Create a review view for the named enrichment
        from ..db_operations import create_run_view

        docs_table = source_table or config_data.get('default_table', 'documents')
        key_col = config_data.get('key_column', 'sha1')

        # Find the most recent prompt_hash for this enrichment
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT prompt_hash, MAX(timestamp) as last_run
                FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = ?
                GROUP BY prompt_hash
                ORDER BY last_run DESC
            """, (name,))
            runs = cursor.fetchall()

        if not runs:
            _exit_error(f"No enrichments found for '{name}'.")

        # Show all prompt versions, create view for the latest
        if len(runs) > 1:
            click.echo(f"Found {len(runs)} prompt version(s) for '{name}':")
            for phash, ts in runs:
                click.echo(f"  {phash[:8]}  (last run: {ts})")
            click.echo(f"Creating view for latest version ({runs[0][0][:8]})...\n")

        prompt_hash = runs[0][0]
        run_ts = runs[0][1]

        view_name = create_run_view(
            db_path=db_path,
            enrichment_name=name,
            prompt_id=prompt_hash,
            run_timestamp=run_ts,
            documents_table=docs_table,
            key_column=key_col,
        )
        if not view_name:
            _exit_error(f"No enrichment fields found for '{name}'; no view was created.")

        # Show what was created
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({view_name})")
            cols = [row[1] for row in cursor.fetchall()]
            cursor.execute(f"SELECT COUNT(*) FROM {view_name}")
            row_count = cursor.fetchone()[0]

        click.echo(f"Created view: {view_name}")
        click.echo(f"  {row_count} rows, columns: {', '.join(cols)}")
        click.echo(f"\nQuery it:")
        click.echo(f"  doctrail query \"SELECT * FROM {view_name} LIMIT 20\"")

    elif action == 'pivot':
        if not name:
            _exit_error("Usage: doctrail view pivot <view_name> -e <enrichment>")
        if not enrichment:
            # List available enrichments to help the user
            with get_db_connection(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT enrichment_name, COUNT(DISTINCT key_value) as rows,
                           COUNT(DISTINCT field_name) as fields
                    FROM {ENRICHMENTS_TABLE}
                    GROUP BY enrichment_name
                    ORDER BY enrichment_name
                """)
                enrichments_list = cursor.fetchall()

            if not enrichments_list:
                _exit_error("No enrichments in database yet.")

            click.echo("Missing -e/--enrichment. Available enrichments:\n")
            for ename, row_count, field_count in enrichments_list:
                click.echo(f"  {ename}  ({row_count} rows, {field_count} fields)")
            click.echo(f"\nUsage: doctrail view pivot {name} -e <enrichment_name>")
            raise click.exceptions.Exit(1)

        from ..db_operations import create_pivot_view

        docs_table = source_table or config_data.get('default_table', 'documents')
        key_col = config_data.get('key_column', 'sha1')
        fields_list = [f.strip() for f in fields.split(',')] if fields else None
        include_list = [c.strip() for c in include.split(',')] if include else None

        try:
            result = create_pivot_view(
                db_path=db_path,
                view_name=name,
                enrichment_name=enrichment,
                documents_table=docs_table,
                key_column=key_col,
                fields=fields_list,
                include_columns=include_list,
                by_model=by_model,
            )
        except ValueError as e:
            _exit_error(f"Error: {e}")

        click.echo(f"Created view: {result['view_name']}")
        click.echo(f"  {result['row_count']} rows, {len(result['columns'])} columns")
        click.echo(f"  Columns: {', '.join(result['columns'])}")

        if result.get('model_legend'):
            click.echo(f"\n  Model legend:")
            for model_name, prefix in sorted(result['model_legend'].items(), key=lambda x: x[1]):
                click.echo(f"    {prefix} = {model_name}")

        click.echo(f"\nQuery it:")
        click.echo(f"  doctrail query \"SELECT * FROM {result['view_name']} LIMIT 20\"")

    elif action == 'render':
        if not name:
            _exit_error("Usage: doctrail view render <view_name> --output <file.html>")

        from ..db_operations import render_view_output

        output_path = output or str(Path.cwd() / f"{name}.{render_format}")

        try:
            result = render_view_output(
                db_path=db_path,
                view_name=name,
                output_path=output_path,
                format_name=render_format,
                limit=render_limit,
                title=name,
            )
        except Exception as e:
            _exit_error(f"Error: {e}")

        click.echo(f"Rendered {name} -> {result['output_path']}")
        click.echo(f"  Format: {result['format']}")
        click.echo(f"  Rows: {result['row_count']}")
        click.echo(f"  Columns: {', '.join(result['columns'])}")

    elif action == 'refresh':
        if not views_dir.exists():
            _exit_error("No .doctrail/views/ folder. Create views with: doctrail view new <name>")

        sql_files = list(views_dir.glob("*.sql"))
        yaml_files = sorted(list(views_dir.glob("*.yml")) + list(views_dir.glob("*.yaml")))
        if not sql_files and not yaml_files:
            _exit_error("No SQL or YAML view definitions in .doctrail/views/")

        click.echo(f"Refreshing {len(sql_files)} SQL view(s) and {len(yaml_files)} YAML spec(s)...")
        success = 0
        for sql_file in sql_files:
            try:
                sql = sql_file.read_text()
                with get_db_connection(db_path) as conn:
                    conn.executescript(sql)
                click.echo(f"  ✓ {sql_file.stem}")
                success += 1
            except Exception as e:
                click.echo(f"  ✗ {sql_file.stem}: {e}", err=True)

        if yaml_files:
            from ..db_operations import create_view_from_spec

            for spec_file in yaml_files:
                try:
                    spec_data = yaml.safe_load(spec_file.read_text()) or {}
                    if not isinstance(spec_data, dict):
                        raise ValueError("View spec must be a YAML mapping")
                    spec_data.setdefault('name', spec_file.stem)
                    create_view_from_spec(
                        db_path=db_path,
                        spec=spec_data,
                        default_source_table=source_table or config_data.get('default_table', 'documents'),
                        default_key_column=config_data.get('key_column', 'sha1'),
                    )
                    click.echo(f"  ✓ {spec_file.stem}")
                    success += 1
                except Exception as e:
                    click.echo(f"  ✗ {spec_file.stem}: {e}", err=True)

        total = len(sql_files) + len(yaml_files)
        click.echo(f"\n{success}/{total} views updated in database")
