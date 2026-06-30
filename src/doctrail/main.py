"""Backward-compatible CLI module for ``python -m doctrail.main`` and direct script use."""

import os
import sys

if __package__:
    from .cli import cli
else:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    if script_dir in sys.path:
        sys.path.remove(script_dir)
    sys.path.insert(0, project_root)
    from doctrail.cli import cli

__all__ = ['cli']


if __name__ == '__main__':
    cli()
