"""Simple error handling with fuzzy matching and concise output."""

import click
import difflib
import re
from typing import List, Optional, Tuple
import sys

def handle_enrichment_error(requested: List[str], available: List[str]) -> str:
    """Handle enrichment not found errors with fuzzy matching."""
    error_parts = []
    
    # Find closest matches for each requested enrichment
    suggestions = []
    for req in requested:
        closest = difflib.get_close_matches(req, available, n=1, cutoff=0.4)
        if closest:
            suggestions.append((req, closest[0]))
    
    # If we have a single close match, make it prominent
    if len(suggestions) == 1:
        req, match = suggestions[0]
        error_parts.append(click.style(f"Did you mean '{match}'?", fg='yellow', bold=True))
    elif len(suggestions) > 1:
        error_parts.append(click.style("Did you mean:", fg='yellow', bold=True))
        for req, match in suggestions:
            error_parts.append(click.style(f"  {req} → {match}", fg='green'))
    
    # Show available enrichments
    error_parts.append(f"\nAvailable enrichments: {', '.join(available)}")
    
    # Add help hint
    error_parts.append(click.style("\nFor full help: doctrail enrich --help", fg='cyan', dim=True))
    
    return '\n'.join(error_parts)

def handle_cli_error(e: Exception, available_commands: List[str] = None) -> None:
    """Handle CLI errors with simple, helpful output."""
    if available_commands is None:
        available_commands = ['enrich', 'ingest', 'export']
    
    error_msg = str(e).replace("Error: ", "").strip()
    
    # Check for common command typos
    command_match = re.search(r"No such command '(\w+)'", error_msg)
    if command_match:
        attempted_cmd = command_match.group(1)
        closest = difflib.get_close_matches(attempted_cmd, available_commands, n=1, cutoff=0.5)
        
        if closest:
            click.echo(click.style(f"Did you mean '{closest[0]}'?", fg='yellow', bold=True), err=True)
        
        click.echo(f"\nAvailable commands: {', '.join(available_commands)}", err=True)
        click.echo(click.style("For help: doctrail --help", fg='cyan', dim=True), err=True)
        return
    
    # Check for missing arguments
    if "Missing option" in error_msg or "Missing parameter" in error_msg:
        argv = ' '.join(sys.argv)
        
        if 'enrich' in argv:
            if '--config' not in argv:
                click.echo(click.style("Missing --config", fg='red', bold=True), err=True)
                click.echo("Try: doctrail enrich --config config.yml --enrichments task_name", err=True)
            elif '--enrichments' not in argv:
                click.echo(click.style("Missing --enrichments", fg='red', bold=True), err=True)
                click.echo("Try: doctrail enrich --config config.yml --enrichments task_name", err=True)
        elif 'ingest' in argv:
            if '--db-path' not in argv and '--config' not in argv:
                click.echo(click.style("Missing --db-path or --config", fg='red', bold=True), err=True)
                click.echo("Try: doctrail ingest --db-path ./database.db --input-dir ./docs", err=True)
            elif '--input-dir' not in argv and '--zotero' not in argv and '--plugin' not in argv:
                click.echo(click.style("Missing input source", fg='red', bold=True), err=True)
                click.echo("Try one of:", err=True)
                click.echo("  doctrail ingest --db-path ./database.db --input-dir ./docs", err=True)
                click.echo("  doctrail ingest --db-path ./database.db --zotero --collection name", err=True)
        elif 'export' in argv:
            if '--config' not in argv:
                click.echo(click.style("Missing --config", fg='red', bold=True), err=True)
                click.echo("Try: doctrail export --config config.yml --export-type parallel-translation", err=True)
            elif '--export-type' not in argv:
                click.echo(click.style("Missing --export-type", fg='red', bold=True), err=True)
                click.echo("Try: doctrail export --config config.yml --export-type parallel-translation", err=True)
        
        click.echo(click.style("\nFor help: doctrail COMMAND --help", fg='cyan', dim=True), err=True)
        return
    
    # Default error display
    click.echo(click.style(f"Error: {error_msg}", fg='red'), err=True)
    click.echo(click.style("For help: doctrail --help", fg='cyan', dim=True), err=True)