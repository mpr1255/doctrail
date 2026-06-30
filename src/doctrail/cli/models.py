"""CLI command for listing doctrail model identifiers."""

from __future__ import annotations

import json

import click

from ..utils import cost_estimation as cost_utils
from ..utils import model_pricing as pricing_utils
from .main import cli

FEATURED_OPENAI_MODELS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o3-mini",
    "o4-mini",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
]

FEATURED_ANTHROPIC_MODELS = [
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5",
    "claude-opus-4-1",
    "claude-opus-4",
    "claude-3-haiku",
]

FEATURED_GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-pro-latest",
    "gemini-flash-latest",
]

FEATURED_CLI_CLAUDE_MODELS = [
    "cli/claude/sonnet",
    "cli/claude/opus",
    "cli/claude/haiku",
]

FEATURED_CLI_GEMINI_MODELS = [
    "cli/gemini/gemini-3.5-flash",
    "cli/gemini/gemini-2.5-flash",
    "cli/gemini/gemini-2.5-pro",
    "cli/gemini/gemini-2.0-flash",
]

FEATURED_CLI_CODEX_MODELS = [
    "cli/codex/gpt-5.5",
    "cli/codex/gpt-5.4",
    "cli/codex/gpt-5",
    "cli/codex/gpt-5-mini",
    "cli/codex/gpt-5.3-codex",
    "cli/codex/gpt-5.2-codex",
    "cli/codex/o3-mini",
]

FEATURED_OPENROUTER_OPENAI = [
    "openai/gpt-5-mini",
    "openai/gpt-4o-mini",
    "openai/o3",
]

FEATURED_OPENROUTER_ANTHROPIC = [
    "anthropic/claude-sonnet-4",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-opus-4",
]

FEATURED_OPENROUTER_GOOGLE = [
    "google/gemini-3.5-flash",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "google/gemini-2.0-flash",
]


def _dedupe_preserve_order(values):
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _is_openai_model(model_id: str) -> bool:
    return model_id.startswith("gpt-") or model_id.startswith("o")


def _normalize_anthropic_model(model_id: str) -> str:
    normalized = pricing_utils.canonicalize_model_name(model_id)
    return normalized.removeprefix("anthropic/")


def _is_anthropic_model(model_id: str) -> bool:
    return _normalize_anthropic_model(model_id).startswith("claude")


def _normalize_gemini_model(model_id: str) -> str:
    return model_id.removeprefix("models/")


def _is_gemini_model(model_id: str) -> bool:
    return _normalize_gemini_model(model_id).startswith("gemini")


def _collect_direct_models(featured, known_models, predicate, normalize=None):
    normalize = normalize or (lambda value: value)
    known = {normalize(model_id) for model_id in known_models if predicate(normalize(model_id))}
    ordered_featured = [model_id for model_id in featured if model_id in known]
    remainder = sorted(known - set(ordered_featured))
    return _dedupe_preserve_order(ordered_featured + remainder)


def _collect_openrouter_models(provider_prefix, featured, predicate):
    cache_models = pricing_utils.get_all_models()
    available = {
        model_id
        for model_id in cache_models
        if model_id.startswith(f"{provider_prefix}/") and predicate(model_id.split("/", 1)[1])
    }
    ordered_featured = [model_id for model_id in featured if model_id in available]
    curated = ordered_featured or sorted(available)[: len(featured)]
    return [f"openrouter/{model_id}" for model_id in _dedupe_preserve_order(curated)]


def _matches_search(model_id: str, search: str | None) -> bool:
    if not search:
        return True
    return search.lower() in model_id.lower()


def _matches_provider(section, provider: str | None) -> bool:
    if not provider:
        return True
    return provider.lower() in section["aliases"]


def _build_curated_sections():
    return [
        {
            "id": "openai",
            "group": "Direct providers",
            "title": "OpenAI",
            "aliases": {"openai"},
            "models": _collect_direct_models(
                FEATURED_OPENAI_MODELS,
                cost_utils.KNOWN_OPENAI_MODELS,
                _is_openai_model,
            ),
        },
        {
            "id": "anthropic",
            "group": "Direct providers",
            "title": "Anthropic",
            "aliases": {"anthropic", "claude"},
            "models": _collect_direct_models(
                FEATURED_ANTHROPIC_MODELS,
                cost_utils.KNOWN_ANTHROPIC_MODELS,
                _is_anthropic_model,
                normalize=_normalize_anthropic_model,
            ),
        },
        {
            "id": "gemini",
            "group": "Direct providers",
            "title": "Gemini",
            "aliases": {"gemini", "google"},
            "models": _collect_direct_models(
                FEATURED_GEMINI_MODELS,
                cost_utils.KNOWN_GEMINI_MODELS,
                _is_gemini_model,
                normalize=_normalize_gemini_model,
            ),
        },
        {
            "id": "cli/claude",
            "group": "CLI backends",
            "title": "Claude CLI",
            "aliases": {"cli", "cli/claude"},
            "models": FEATURED_CLI_CLAUDE_MODELS,
        },
        {
            "id": "cli/gemini",
            "group": "CLI backends",
            "title": "Gemini CLI",
            "aliases": {"cli", "cli/gemini"},
            "models": FEATURED_CLI_GEMINI_MODELS,
        },
        {
            "id": "cli/codex",
            "group": "CLI backends",
            "title": "Codex CLI",
            "aliases": {"cli", "cli/codex", "codex"},
            "models": FEATURED_CLI_CODEX_MODELS,
        },
        {
            "id": "openrouter/openai",
            "group": "OpenRouter",
            "title": "OpenAI via OpenRouter",
            "aliases": {"openrouter", "openrouter/openai"},
            "models": _collect_openrouter_models("openai", FEATURED_OPENROUTER_OPENAI, _is_openai_model),
        },
        {
            "id": "openrouter/anthropic",
            "group": "OpenRouter",
            "title": "Anthropic via OpenRouter",
            "aliases": {"openrouter", "openrouter/anthropic"},
            "models": _collect_openrouter_models("anthropic", FEATURED_OPENROUTER_ANTHROPIC, _is_anthropic_model),
        },
        {
            "id": "openrouter/google",
            "group": "OpenRouter",
            "title": "Gemini via OpenRouter",
            "aliases": {"openrouter", "openrouter/google", "openrouter/gemini"},
            "models": _collect_openrouter_models("google", FEATURED_OPENROUTER_GOOGLE, _is_gemini_model),
        },
    ]


def _filter_curated_sections(provider: str | None, search: str | None):
    sections = []
    for section in _build_curated_sections():
        if not _matches_provider(section, provider):
            continue
        filtered_models = [model_id for model_id in section["models"] if _matches_search(model_id, search)]
        if not filtered_models:
            continue
        sections.append({**section, "models": filtered_models})
    return sections


def _render_curated_text(sections, limit: int, cache_info):
    if not sections:
        click.echo("No models found matching your filters.")
        return

    click.echo("Main doctrail model IDs")
    click.echo("Use these exact values with --model.")

    current_group = None
    for section in sections:
        if section["group"] != current_group:
            click.echo()
            click.echo(section["group"])
            current_group = section["group"]

        click.echo(f"  {section['title']}")
        shown = section["models"][:limit]
        for model_id in shown:
            click.echo(f"    {model_id}")
        remaining = len(section["models"]) - len(shown)
        if remaining > 0:
            click.echo(f"    ... {remaining} more")

    click.echo()
    click.echo(f"Showing up to {limit} model IDs per section.")
    click.echo("Use 'doctrail models --all' for the full OpenRouter catalog with pricing.")

    if cache_info["age_hours"] is not None:
        click.echo(
            f"OpenRouter cache: {cache_info['model_count']:,} models, {cache_info['age_hours']:.1f}h old"
        )
    if cache_info["is_bootstrap"]:
        click.echo("OpenRouter sections are using bootstrap fallback. Run 'doctrail models --refresh' for the full catalog.")


def _render_curated_json(sections, limit: int, cache_info):
    payload = {
        "mode": "curated",
        "sections": [
            {
                "id": section["id"],
                "group": section["group"],
                "title": section["title"],
                "models": section["models"][:limit],
                "total": len(section["models"]),
            }
            for section in sections
        ],
        "cache": cache_info,
    }
    click.echo(json.dumps(payload, indent=2))


def _get_openai_batch_items(search: str | None):
    items = []
    for model_id, info in pricing_utils.get_openai_batch_models().items():
        if search and search.lower() not in model_id.lower():
            continue
        items.append((model_id, info))
    items.sort(key=lambda item: item[0])
    return items


def _render_openai_batch_table(items, limit: int, cache_info):
    if not items:
        click.echo("No OpenAI batch models found matching your filters.")
        return

    shown_items = items[:limit]
    id_width = min(max(len(model_id) for model_id, _ in shown_items), 24)
    snapshot_width = min(
        max(len((info.get("snapshots") or ["-"])[0]) for _, info in shown_items),
        24,
    )

    click.echo(
        f"\n{'Model ID':<{id_width}}  {'Input/1M':>10}  {'Cached/1M':>10}  {'Output/1M':>10}  {'Snapshot':<{snapshot_width}}"
    )
    click.echo(f"{'─' * id_width}  {'─' * 10}  {'─' * 10}  {'─' * 10}  {'─' * snapshot_width}")

    for model_id, info in shown_items:
        cached_input = info.get("batch_cached_input")
        snapshot = (info.get("snapshots") or ["-"])[0]
        cached_str = f"${cached_input:>8.3f}" if cached_input is not None else "         -"
        click.echo(
            f"{model_id:<{id_width}}  "
            f"${info['batch_input']:>8.3f}  "
            f"{cached_str}  "
            f"${info['batch_output']:>8.3f}  "
            f"{snapshot:<{snapshot_width}}"
        )

    click.echo()
    click.echo(f"Showing {len(shown_items)} of {len(items)} OpenAI batch models", nl=False)
    if len(items) > len(shown_items):
        click.echo(" (use --limit to see more)", nl=False)
    click.echo()

    if cache_info["age_hours"] is not None:
        click.echo(
            f"OpenAI batch catalog: {cache_info['model_count']:,} models, "
            f"{cache_info['age_hours']:.1f}h old"
        )
    if cache_info.get("fetched_at"):
        click.echo(f"Fetched from OpenAI docs: {cache_info['fetched_at']}")
    if cache_info["is_bootstrap"]:
        click.echo("Using bootstrap fallback. Run 'doctrail models --openai-batch --refresh' to fetch the current catalog.")


def _render_openai_batch_json(items, limit: int, cache_info):
    payload = {
        "mode": "openai_batch",
        "models": {
            model_id: info
            for model_id, info in items[:limit]
        },
        "cache": cache_info,
    }
    click.echo(json.dumps(payload, indent=2))


def _normalize_openrouter_provider(provider: str | None) -> str | None:
    if not provider:
        return None

    provider = provider.lower()
    mapping = {
        "openai": "openai",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "gemini": "google",
        "google": "google",
        "openrouter/openai": "openai",
        "openrouter/anthropic": "anthropic",
        "openrouter/google": "google",
        "openrouter/gemini": "google",
        "openrouter": None,
    }
    return mapping.get(provider, provider)


def _get_openrouter_items(provider: str | None, search: str | None):
    provider_filter = _normalize_openrouter_provider(provider)
    items = []
    for raw_model_id, info in pricing_utils.get_all_models().items():
        raw_provider = raw_model_id.split("/", 1)[0]
        if provider_filter and raw_provider != provider_filter:
            continue

        display_model_id = f"openrouter/{raw_model_id}"
        if search:
            search_lower = search.lower()
            if search_lower not in display_model_id.lower() and search_lower not in info.get("name", "").lower():
                continue
        items.append((display_model_id, info))

    items.sort(key=lambda item: item[0])
    return items


def _render_openrouter_table(items, limit: int, cache_info):
    if not items:
        click.echo("No models found matching your filters.")
        return

    shown_items = items[:limit]
    id_width = min(max(len(model_id) for model_id, _ in shown_items), 60)
    name_width = min(max(len(info.get("name", "")[:35]) for _, info in shown_items), 35)

    click.echo(
        f"\n{'Model ID':<{id_width}}  {'Name':<{name_width}}  {'Input/1M':>10}  {'Output/1M':>10}  {'Context':>9}"
    )
    click.echo(f"{'─' * id_width}  {'─' * name_width}  {'─' * 10}  {'─' * 10}  {'─' * 9}")

    for model_id, info in shown_items:
        name = info.get("name", "")[:name_width]
        input_price = info.get("input", 0)
        output_price = info.get("output", 0)
        context_length = info.get("context_length")
        context_str = f"{context_length:>9,}" if context_length else "        ?"
        input_str = f"${input_price:>8.4f}" if input_price > 0 else "    free"
        output_str = f"${output_price:>8.4f}" if output_price > 0 else "    free"
        click.echo(
            f"{model_id:<{id_width}}  {name:<{name_width}}  {input_str}  {output_str}  {context_str}"
        )

    click.echo()
    click.echo(f"Showing {len(shown_items)} of {len(items)} OpenRouter models", nl=False)
    if len(items) > len(shown_items):
        click.echo(" (use --limit to see more)", nl=False)
    click.echo()

    if cache_info["age_hours"] is not None:
        click.echo(f"Cache: {cache_info['model_count']:,} models, {cache_info['age_hours']:.1f}h old")
    if cache_info["is_bootstrap"]:
        click.echo("Using bootstrap fallback. Run 'doctrail models --refresh' to fetch the full catalog.")


def _render_openrouter_json(items, limit: int):
    payload = {model_id: info for model_id, info in items[:limit]}
    click.echo(json.dumps(payload, indent=2))


@cli.command()
@click.option('--provider', '-p', help='Filter by backend (e.g. openai, anthropic, gemini, cli, openrouter/openai)')
@click.option('--search', '-s', help='Search models by name or identifier')
@click.option('--refresh', is_flag=True, help='Force refresh the underlying pricing or batch catalog cache')
@click.option('--limit', '-n', default=10, show_default=True, help='Max models to display per section')
@click.option('--all', 'show_all', is_flag=True, help='Show the full OpenRouter catalog with pricing')
@click.option('--openai-batch', is_flag=True, help='Show the verified OpenAI batch model catalog and batch pricing')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
def models(provider, search, refresh, limit, show_all, openai_batch, as_json):
    """List doctrail model identifiers by backend."""
    if refresh:
        if openai_batch:
            click.echo("Refreshing OpenAI batch catalog from official OpenAI docs...")
            count = pricing_utils.refresh_openai_batch_catalog()
            if count:
                click.echo(f"Cached {count:,} OpenAI batch models.")
            else:
                click.echo("Failed to refresh OpenAI batch catalog. Continuing with existing or bootstrap data.", err=True)
        else:
            click.echo("Refreshing pricing cache from OpenRouter...")
            count = pricing_utils.refresh_cache()
            if count:
                click.echo(f"Cached pricing for {count:,} models.")
            else:
                click.echo("Failed to refresh cache. Continuing with existing or bootstrap data.", err=True)

    if openai_batch:
        cache_info = pricing_utils.get_openai_batch_cache_info()
        items = _get_openai_batch_items(search)
        if as_json:
            _render_openai_batch_json(items, limit, cache_info)
        else:
            _render_openai_batch_table(items, limit, cache_info)
        return

    cache_info = pricing_utils.get_cache_info()

    if show_all:
        items = _get_openrouter_items(provider, search)
        if as_json:
            _render_openrouter_json(items, limit)
        else:
            _render_openrouter_table(items, limit, cache_info)
        return

    sections = _filter_curated_sections(provider, search)
    if as_json:
        _render_curated_json(sections, limit, cache_info)
    else:
        _render_curated_text(sections, limit, cache_info)
