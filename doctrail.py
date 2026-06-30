#!/usr/bin/env python3
"""Doctrail CLI entry point for development."""

import os
import sys
from pathlib import Path


_PACKAGE_DIR = Path(__file__).resolve().parent / "src" / "doctrail"
if _PACKAGE_DIR.exists():
    __path__ = [str(_PACKAGE_DIR)]


def _load_cli():
    try:
        from doctrail.cli import cli
    except ModuleNotFoundError:
        if os.environ.get("DOCTRAIL_UV_REEXEC") == "1":
            raise
        project_root = Path(__file__).resolve().parent
        os.environ["DOCTRAIL_UV_REEXEC"] = "1"
        os.execvp(
            "uv",
            [
                "uv",
                "run",
                "--project",
                str(project_root),
                "python3",
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
        )
    return cli


if __name__ == '__main__':
    _load_cli()()
