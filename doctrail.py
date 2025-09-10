#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.*",
#     "pyyaml==6.*",
#     "tqdm==4.*",
#     "openai==1.*",
#     "pydantic==2.*",
#     "jinja2==3.*",
#     "tika==2.*",
#     "tiktoken",
#     "beautifulsoup4==4.*",
#     "chardet==5.*",
#     "aiohttp==3.*",
#     "loguru",
#     "typer",
#     "sqlite_utils",
#     "pyzotero",
#     "readability-lxml",
#     "mhtml-to-html-py",
#     "google-genai",
#     "pypinyin",
# ]
# ///

# Suppress the pkg_resources deprecation warning from tika
import warnings
warnings.filterwarnings('ignore', message='.*pkg_resources is deprecated.*')

from src.main import cli, show_main_help
from src.utils.simple_error_handler import handle_cli_error
import sys
import click

if __name__ == "__main__":
    try:
        # standalone_mode=False prevents Click from handling exceptions automatically
        # and exiting, allowing our custom handler below.
        cli(standalone_mode=False)
    except (click.ClickException, click.UsageError) as e:
        # Use our enhanced error handler
        handle_cli_error(e)
        sys.exit(1)
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully - message already printed by signal handler
        sys.exit(130)  # Standard exit code for Ctrl+C
    except Exception as e:
        # Catch other unexpected errors
        click.echo(f"Unhandled error: {str(e)}", err=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
