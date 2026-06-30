#!/usr/bin/env python3
"""Build docs/llms.txt from the docs home, mkdocs nav order, and CLI reference."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import click
import yaml


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
OUTPUT = DOCS / "llms.txt"
INDEX_DOC = "index.md"
CLI_DOC = "cli.md"


if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def iter_nav_paths(items: Iterable[object]) -> Iterable[str]:
    for item in items:
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    yield from iter_nav_paths(value)


def iter_manual_paths(items: Iterable[object]) -> Iterable[str]:
    seen: set[str] = set()
    if (DOCS / INDEX_DOC).exists():
        seen.add(INDEX_DOC)
        yield INDEX_DOC
    for path in iter_nav_paths(items):
        if path.endswith(".md") and path not in seen:
            seen.add(path)
            yield path


class _MkdocsLoader(yaml.SafeLoader):
    """SafeLoader that tolerates mkdocs' python-object tags (e.g. mermaid fences)."""


_MkdocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: None
)


def _format_click_help(command: click.Command, prog_name: str) -> str:
    ctx = click.Context(command, info_name=prog_name, color=False)
    formatter = click.HelpFormatter(width=100)
    command.format_help(ctx, formatter)
    return formatter.getvalue().rstrip()


def _iter_click_commands(command: click.Command, prog_name: str) -> Iterable[tuple[str, click.Command]]:
    yield prog_name, command
    if not isinstance(command, click.Group):
        return

    ctx = click.Context(command, info_name=prog_name, color=False)
    for name in command.list_commands(ctx):
        subcommand = command.get_command(ctx, name)
        if subcommand is None or subcommand.hidden:
            continue
        yield from _iter_click_commands(subcommand, f"{prog_name} {name}")


def render_cli_reference() -> str:
    from doctrail.cli import cli

    parts = ["# CLI", "", "Generated from the live Click command tree.", ""]
    for prog_name, command in _iter_click_commands(cli, "doctrail"):
        heading_level = "##" if prog_name == "doctrail" else "###"
        parts.extend([
            f"{heading_level} {prog_name}",
            "",
            "```text",
            _format_click_help(command, prog_name),
            "```",
            "",
        ])
    return "\n".join(parts).rstrip()


def main() -> None:
    config = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_MkdocsLoader)
    manual_paths = list(iter_manual_paths(config.get("nav", [])))

    parts = [
        "# Doctrail full manual",
        "",
        "Generated from mkdocs navigation by `scripts/build_llms_full.py`.",
        "",
    ]

    for rel_path in manual_paths:
        doc_path = DOCS / rel_path
        if rel_path == CLI_DOC:
            text = render_cli_reference()
        else:
            text = doc_path.read_text(encoding="utf-8").strip()
        parts.extend([f"<!-- {rel_path} -->", "", text, ""])

    OUTPUT.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
