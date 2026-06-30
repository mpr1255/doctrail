#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "rich",
# ]
# ///

"""
Test script for Doctrail API server.
"""

import httpx
import asyncio
import os
import pytest
from rich.console import Console
from rich.panel import Panel
from rich.json import JSON

console = Console()

pytestmark = pytest.mark.skipif(
    os.environ.get("DOCTRAIL_RUN_LIVE_API_TESTS") != "1",
    reason="manual live API demo; set DOCTRAIL_RUN_LIVE_API_TESTS=1 to run",
)

async def test_health():
    """Test health endpoint."""
    console.print("\n[bold cyan]Testing Health Endpoint[/bold cyan]")

    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8000/health")
        console.print(Panel(JSON(response.text), title="GET /health"))
        return response.status_code == 200

async def test_enrich(enrichment_name: str, limit: int = 2):
    """Test enrichment endpoint."""
    console.print(f"\n[bold cyan]Testing Enrichment: {enrichment_name}[/bold cyan]")

    request_data = {
        "config_path": "/tmp/doctrail_demo/test_config.yml",
        "enrichments": [enrichment_name],
        "limit": limit,
        "skip_cost_check": True,
        "verbose": True
    }

    console.print("\n[yellow]Request:[/yellow]")
    console.print(Panel(JSON.from_data(request_data), title="POST /enrich"))

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "http://localhost:8000/enrich",
            json=request_data
        )

        console.print(f"\n[green]Status: {response.status_code}[/green]")

        if response.status_code == 200:
            result = response.json()
            console.print("\n[yellow]Response:[/yellow]")
            console.print(f"  Status: {result['status']}")
            console.print(f"  Enrichments run: {result['enrichments_run']}")
            console.print(f"  Total processed: {result['total_processed']}")

            if result.get('errors'):
                console.print(f"  [red]Errors: {result['errors']}[/red]")
        else:
            console.print(f"[red]Error: {response.text}[/red]")

        return response.status_code == 200

async def check_database():
    """Check database after enrichment."""
    console.print("\n[bold cyan]Checking Database Results[/bold cyan]")

    import subprocess
    result = subprocess.run(
        [
            "uv", "run", "sqlite-utils", "query",
            "/tmp/doctrail_demo/test.db",
            "SELECT filename, language, substr(summary, 1, 80) as summary_preview, topics FROM documents",
            "--table"
        ],
        capture_output=True,
        text=True
    )
    console.print(result.stdout)

async def main():
    """Run all tests."""
    console.print("[bold magenta]Doctrail API Test Suite[/bold magenta]")
    console.print("Server: http://localhost:8000")

    # Test health
    if not await test_health():
        console.print("[red]Server not responding.[/red]")
        console.print("[yellow]Start server with: uv run uvicorn doctrail.server:app --reload[/yellow]")
        return

    console.print("[green]Server is running[/green]")

    # Test enrichments
    console.print("\n" + "="*60)
    await test_enrich("detect_language", limit=4)

    console.print("\n" + "="*60)
    await check_database()

    # Test another enrichment
    console.print("\n" + "="*60)
    await test_enrich("summarize", limit=4)

    console.print("\n" + "="*60)
    await check_database()

if __name__ == "__main__":
    asyncio.run(main())
