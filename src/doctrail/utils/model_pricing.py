"""
Model pricing and batch catalog metadata for doctrail.

OpenRouter pricing is fetched from OpenRouter's public models endpoint.
OpenAI batch model availability and batch pricing are fetched from the
official OpenAI model pages.
"""

import html
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_DIR = Path.home() / ".cache" / "doctrail"
CACHE_FILE = CACHE_DIR / "model_pricing.json"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
OPENAI_BATCH_CACHE_FILE = CACHE_DIR / "openai_batch_catalog.json"
OPENAI_MODELS_INDEX_URL = "https://developers.openai.com/api/docs/models/all"
OPENAI_BATCH_DOC_URL = "https://developers.openai.com/api/docs/models/{model}"

_OPENAI_BATCH_BOOTSTRAP_MODELS = {
    "gpt-5.4": {
        "batch_input": 2.50,
        "batch_cached_input": 0.25,
        "batch_output": 15.00,
        "snapshots": ["gpt-5.4-2026-03-05"],
    },
    "gpt-5.4-pro": {
        "batch_input": 30.00,
        "batch_cached_input": None,
        "batch_output": 180.00,
        "snapshots": ["gpt-5.4-pro-2026-03-05"],
    },
    "gpt-5.1": {
        "batch_input": 1.25,
        "batch_cached_input": 0.125,
        "batch_output": 10.00,
        "snapshots": ["gpt-5.1-2025-11-13"],
    },
    "gpt-5": {
        "batch_input": 1.25,
        "batch_cached_input": 0.125,
        "batch_output": 10.00,
        "snapshots": ["gpt-5-2025-08-07"],
    },
    "gpt-5-mini": {
        "batch_input": 0.25,
        "batch_cached_input": 0.025,
        "batch_output": 2.00,
        "snapshots": ["gpt-5-mini-2025-08-07"],
    },
    "gpt-5-nano": {
        "batch_input": 0.05,
        "batch_cached_input": 0.005,
        "batch_output": 0.40,
        "snapshots": ["gpt-5-nano-2025-08-07"],
    },
    "gpt-4.1": {
        "batch_input": 2.00,
        "batch_cached_input": 0.50,
        "batch_output": 8.00,
        "snapshots": ["gpt-4.1-2025-04-14"],
    },
    "gpt-4.1-mini": {
        "batch_input": 0.40,
        "batch_cached_input": 0.10,
        "batch_output": 1.60,
        "snapshots": ["gpt-4.1-mini-2025-04-14"],
    },
    "gpt-4.1-nano": {
        "batch_input": 0.10,
        "batch_cached_input": 0.025,
        "batch_output": 0.40,
        "snapshots": ["gpt-4.1-nano-2025-04-14"],
    },
    "gpt-4o": {
        "batch_input": 2.50,
        "batch_cached_input": 1.25,
        "batch_output": 10.00,
        "snapshots": ["gpt-4o-2024-08-06", "gpt-4o-2024-11-20", "gpt-4o-2024-05-13"],
    },
    "gpt-4o-mini": {
        "batch_input": 0.15,
        "batch_cached_input": 0.075,
        "batch_output": 0.60,
        "snapshots": ["gpt-4o-mini-2024-07-18"],
    },
    "o4-mini": {
        "batch_input": 1.10,
        "batch_cached_input": 0.275,
        "batch_output": 4.40,
        "snapshots": ["o4-mini-2025-04-16"],
    },
    "o3": {
        "batch_input": 2.00,
        "batch_cached_input": 0.50,
        "batch_output": 8.00,
        "snapshots": ["o3-2025-04-16"],
    },
    "o3-mini": {
        "batch_input": 1.10,
        "batch_cached_input": 0.55,
        "batch_output": 4.40,
        "snapshots": ["o3-mini-2025-01-31"],
    },
    "o3-pro": {
        "batch_input": 20.00,
        "batch_cached_input": None,
        "batch_output": 80.00,
        "snapshots": ["o3-pro-2025-06-10"],
    },
    "o1": {
        "batch_input": 15.00,
        "batch_cached_input": 7.50,
        "batch_output": 60.00,
        "snapshots": ["o1-2024-12-17"],
    },
    "o1-mini": {
        "batch_input": 1.10,
        "batch_cached_input": 0.55,
        "batch_output": 4.40,
        "snapshots": ["o1-mini-2024-09-12"],
    },
    "o1-pro": {
        "batch_input": 150.00,
        "batch_cached_input": None,
        "batch_output": 600.00,
        "snapshots": ["o1-pro-2025-03-19"],
    },
}

_OPENAI_BATCH_PRICE_RE = re.compile(
    r"Batch API price\s*Input\s*\$([0-9]+(?:\.[0-9]+)?)\s*"
    r"(?:Cached input\s*\$([0-9]+(?:\.[0-9]+)?)\s*)?"
    r"Output\s*\$([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.DOTALL,
)
_OPENAI_MODEL_DOC_LINK_RE = re.compile(r"/api/docs/models/([A-Za-z0-9._-]+)")
_OPENAI_MODEL_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9.-]*\d[a-z0-9.-]*\b")

# Bootstrap fallback — used only when no cache exists AND network is down.
# Covers the most common models so first-run offline still works.
_BOOTSTRAP_PRICING = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "o3": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # Anthropic
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    # Google
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-flash-lite": (0.075, 0.30),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-pro-preview": (1.25, 10.00),
    "gemini-3.1-flash-lite-preview": (0.075, 0.30),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-1.5-pro": (1.25, 5.00),
    # DeepSeek
    "deepseek-chat": (0.14, 0.28),
    "deepseek-r1": (0.55, 2.19),
}

# In-memory cache (populated from disk or API)
_pricing_cache: Optional[Dict] = None
_openai_batch_cache: Optional[Dict] = None


def _canonicalize_claude_direct_model(model: str) -> str:
    """Normalize dotted Claude aliases to Anthropic's hyphenated API IDs.

    Anthropic's direct API uses names like `claude-haiku-4-5`, while users
    often type dotted variants such as `claude-haiku-4.5`.
    """
    # Family-first aliases: claude-haiku-4.5 -> claude-haiku-4-5
    model = re.sub(
        r"^(claude-(?:haiku|sonnet|opus)-\d+)\.(\d+)(?=-|$)",
        r"\1-\2",
        model,
    )
    # Version-first aliases: claude-3.5-sonnet -> claude-3-5-sonnet
    model = re.sub(r"^claude-(\d+)\.(\d+)-", r"claude-\1-\2-", model)
    return model


def canonicalize_model_name(model: str) -> str:
    """Normalize user-facing model aliases to backend-safe identifiers.

    Keep OpenRouter names untouched because OpenRouter exposes some Anthropic
    models with dotted IDs. Direct Anthropic and `cli/claude/*` models are
    canonicalized to the hyphenated Anthropic form.
    """
    if not model or model.startswith("openrouter/"):
        return model

    if model.startswith("anthropic/"):
        return "anthropic/" + _canonicalize_claude_direct_model(
            model.removeprefix("anthropic/")
        )

    if model.startswith("cli/claude/"):
        suffix = model.removeprefix("cli/claude/")
        if not suffix:
            return model
        return "cli/claude/" + _canonicalize_claude_direct_model(suffix)

    if model.startswith("claude"):
        return _canonicalize_claude_direct_model(model)

    return model


def _load_cache_from_disk() -> Optional[Dict]:
    """Load cached pricing data from disk if it exists and isn't expired."""
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        cached_at = data.get("cached_at", 0)
        if time.time() - cached_at > CACHE_TTL_SECONDS:
            logger.debug("Pricing cache expired (>24h)")
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Could not read pricing cache: {e}")
        return None


def _load_stale_cache_from_disk() -> Optional[Dict]:
    """Load cache from disk even if expired — for offline fallback."""
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        if "models" in data:
            logger.info("Using stale pricing cache (network unavailable)")
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache_to_disk(data: Dict) -> None:
    """Save pricing data to disk cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data))
        logger.debug(f"Saved pricing cache ({len(data.get('models', {}))} models)")
    except OSError as e:
        logger.warning(f"Could not write pricing cache: {e}")


def _load_named_cache_from_disk(cache_file: Path, ttl_seconds: int) -> Optional[Dict]:
    """Load a cache file if it exists and is younger than the given TTL."""
    try:
        if not cache_file.exists():
            return None
        data = json.loads(cache_file.read_text())
        cached_at = data.get("cached_at", 0)
        if time.time() - cached_at > ttl_seconds:
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Could not read cache {cache_file}: {e}")
        return None


def _load_stale_named_cache_from_disk(cache_file: Path) -> Optional[Dict]:
    """Load a cache file even if expired."""
    try:
        if not cache_file.exists():
            return None
        data = json.loads(cache_file.read_text())
        if "models" in data:
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _save_named_cache_to_disk(cache_file: Path, data: Dict) -> None:
    """Persist an arbitrary cache dict to disk."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data))
    except OSError as e:
        logger.warning(f"Could not write cache {cache_file}: {e}")


def _strip_html_to_text(raw_html: str) -> str:
    """Collapse OpenAI docs HTML into a plain-text search surface."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _fetch_html(url: str, timeout: int = 20) -> Optional[str]:
    """Fetch one OpenAI docs HTML page."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return None


def _discover_openai_model_doc_ids() -> List[str]:
    """Discover model page ids from OpenAI's official models index."""
    raw_html = _fetch_html(OPENAI_MODELS_INDEX_URL)
    if not raw_html:
        return []

    model_ids = _dedupe_preserve_order(_OPENAI_MODEL_DOC_LINK_RE.findall(raw_html))
    return [model_id for model_id in model_ids if model_id != "all"]


def _model_snapshot_prefixes(model_id: str) -> List[str]:
    """Return boundary-safe prefixes used to match documented snapshot ids."""
    prefixes = [model_id]
    parts = model_id.split("-")
    while len(parts) > 2:
        parts = parts[:-1]
        prefixes.append("-".join(parts))
    return _dedupe_preserve_order(prefixes)


def _extract_openai_snapshots(model_id: str, text: str) -> List[str]:
    """Extract documented snapshot ids from the snapshots section of a model page."""
    snapshots_section = text
    snapshots_idx = text.find("Snapshots")
    if snapshots_idx != -1:
        snapshots_section = text[snapshots_idx:]
    rate_limits_idx = snapshots_section.find("Rate limits")
    if rate_limits_idx != -1:
        snapshots_section = snapshots_section[:rate_limits_idx]

    prefixes = _model_snapshot_prefixes(model_id)
    tokens = _OPENAI_MODEL_TOKEN_RE.findall(snapshots_section)
    snapshots = []
    for token in tokens:
        if token == model_id:
            continue
        if any(token == prefix or token.startswith(prefix + "-") for prefix in prefixes):
            snapshots.append(token)
    return _dedupe_preserve_order(snapshots)


def _build_openai_batch_bootstrap_cache() -> Dict:
    """Return the offline fallback catalog for OpenAI batch models."""
    models = {}
    for model_id, entry in _OPENAI_BATCH_BOOTSTRAP_MODELS.items():
        models[model_id] = {
            "id": model_id,
            "batch_input": entry["batch_input"],
            "batch_cached_input": entry["batch_cached_input"],
            "batch_output": entry["batch_output"],
            "supports_batch": True,
            "supports_chat_completions": True,
            "snapshots": entry["snapshots"],
            "source_url": OPENAI_BATCH_DOC_URL.format(model=model_id),
        }

    return {
        "cached_at": 0,
        "fetched_at": "2026-03-14",
        "models": models,
        "is_bootstrap": True,
    }


def _fetch_openai_batch_catalog_entry(model_id: str) -> Optional[Tuple[str, Dict]]:
    """Fetch and parse one OpenAI model page for batch compatibility."""
    url = OPENAI_BATCH_DOC_URL.format(model=model_id)
    raw_html = _fetch_html(url)
    if not raw_html:
        return None

    text = _strip_html_to_text(raw_html)
    supports_batch = "v1/batch" in text
    supports_chat_completions = "v1/chat/completions" in text
    price_match = _OPENAI_BATCH_PRICE_RE.search(text)

    if not supports_batch or not supports_chat_completions or price_match is None:
        return None

    return model_id, {
        "id": model_id,
        "batch_input": float(price_match.group(1)),
        "batch_cached_input": float(price_match.group(2)) if price_match.group(2) else None,
        "batch_output": float(price_match.group(3)),
        "supports_batch": supports_batch,
        "supports_chat_completions": supports_chat_completions,
        "snapshots": _extract_openai_snapshots(model_id, text),
        "source_url": url,
    }


def _fetch_openai_batch_catalog_from_docs() -> Optional[Dict]:
    """Fetch OpenAI batch metadata from official OpenAI model pages."""
    model_ids = _discover_openai_model_doc_ids()
    if not model_ids:
        return None

    fetched_models = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_openai_batch_catalog_entry, model_id): model_id
            for model_id in model_ids
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                logger.debug(f"OpenAI batch page parse failed for {futures[future]}: {exc}")
                continue
            if result is None:
                continue
            model_id, entry = result
            fetched_models[model_id] = entry

    if not fetched_models:
        return None

    data = {
        "cached_at": time.time(),
        "fetched_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "models": fetched_models,
    }
    _save_named_cache_to_disk(OPENAI_BATCH_CACHE_FILE, data)
    return data


def _ensure_openai_batch_cache() -> Dict:
    """Ensure the OpenAI batch metadata cache is populated."""
    global _openai_batch_cache

    if _openai_batch_cache is not None:
        return _openai_batch_cache

    data = _load_named_cache_from_disk(OPENAI_BATCH_CACHE_FILE, CACHE_TTL_SECONDS)
    if data:
        _openai_batch_cache = data
        return _openai_batch_cache

    data = _fetch_openai_batch_catalog_from_docs()
    if data:
        _openai_batch_cache = data
        return _openai_batch_cache

    data = _load_stale_named_cache_from_disk(OPENAI_BATCH_CACHE_FILE)
    if data:
        logger.info("Using stale OpenAI batch catalog (network unavailable)")
        _openai_batch_cache = data
        return _openai_batch_cache

    logger.info("No OpenAI batch catalog available, using bootstrap fallback")
    _openai_batch_cache = _build_openai_batch_bootstrap_cache()
    return _openai_batch_cache


def _fetch_from_openrouter() -> Optional[Dict]:
    """Fetch model data from OpenRouter API. No auth required for this endpoint."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(OPENROUTER_MODELS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.debug(f"Failed to fetch OpenRouter models: {e}")
        return None

    models = {}
    for entry in raw.get("data", []):
        model_id = entry.get("id", "")
        pricing = entry.get("pricing", {})
        # OpenRouter returns pricing as strings in dollars per token
        try:
            input_per_token = float(pricing.get("prompt", "0"))
            output_per_token = float(pricing.get("completion", "0"))
        except (ValueError, TypeError):
            continue

        # Convert per-token to per-1M-tokens
        input_per_1m = input_per_token * 1_000_000
        output_per_1m = output_per_token * 1_000_000

        models[model_id] = {
            "input": round(input_per_1m, 4),
            "output": round(output_per_1m, 4),
            "name": entry.get("name", model_id),
            "context_length": entry.get("context_length"),
        }

    if not models:
        logger.warning("OpenRouter returned no models")
        return None

    data = {"cached_at": time.time(), "models": models}
    _save_cache_to_disk(data)
    logger.debug(f"Fetched pricing for {len(models)} models from OpenRouter")
    return data


def _ensure_cache() -> Dict:
    """Ensure the in-memory cache is populated. Returns the cache dict."""
    global _pricing_cache

    if _pricing_cache is not None:
        return _pricing_cache

    # Try fresh disk cache
    data = _load_cache_from_disk()
    if data:
        _pricing_cache = data
        return _pricing_cache

    # Try fetching from API
    data = _fetch_from_openrouter()
    if data:
        _pricing_cache = data
        return _pricing_cache

    # Try stale disk cache (offline fallback)
    data = _load_stale_cache_from_disk()
    if data:
        _pricing_cache = data
        return _pricing_cache

    # Last resort: bootstrap dict
    logger.info("No pricing cache available, using bootstrap fallback")
    _pricing_cache = {
        "cached_at": 0,
        "models": {
            k: {"input": v[0], "output": v[1], "name": k, "context_length": None}
            for k, v in _BOOTSTRAP_PRICING.items()
        },
        "is_bootstrap": True,
    }
    return _pricing_cache


def _normalize_model_name(model: str) -> List[str]:
    """Generate candidate lookup keys for a model name.

    Doctrail uses several naming conventions:
    - "gpt-4o" (direct OpenAI)
    - "claude-sonnet-4" (direct Anthropic)
    - "gemini-2.0-flash" (direct Google)
    - "openrouter/anthropic/claude-sonnet-4" (OpenRouter prefix)

    OpenRouter uses:
    - "openai/gpt-4o"
    - "anthropic/claude-sonnet-4"
    - "google/gemini-2.0-flash"

    Returns a list of candidate keys to try, in priority order.
    """
    model = canonicalize_model_name(model)
    candidates = []

    # Strip openrouter/ prefix if present
    clean = model.removeprefix("openrouter/")
    candidates.append(clean)

    # If it already has provider/ prefix, also try without it
    if "/" in clean:
        bare = clean.split("/", 1)[1]
        candidates.append(bare)
    else:
        # Try common provider prefixes
        candidates.append(f"openai/{clean}")
        candidates.append(f"anthropic/{clean}")
        candidates.append(f"google/{clean}")
        candidates.append(f"meta-llama/{clean}")
        candidates.append(f"deepseek/{clean}")
        candidates.append(f"mistralai/{clean}")
        candidates.append(f"qwen/{clean}")

    # Strip version suffixes for fallback (e.g. "gpt-4o-2024-08-06" → "gpt-4o")
    for c in list(candidates):
        if "-20" in c:
            candidates.append(c.split("-20")[0])

    # Anthropic uses hyphens (claude-haiku-4-5), OpenRouter uses dots
    # (claude-haiku-4.5). Generate dot variants for any trailing N-M pattern.
    for c in list(candidates):
        m = re.search(r'-(\d+)-(\d+)$', c)
        if m:
            candidates.append(c[:m.start()] + f"-{m.group(1)}.{m.group(2)}")

    # Strip models/ prefix (Gemini sometimes includes it)
    for c in list(candidates):
        if c.startswith("models/"):
            candidates.append(c[7:])

    return candidates


def get_model_price(model: str) -> Tuple[float, float]:
    """Get pricing for a model as (input_per_1m, output_per_1m).

    Looks up the model in the cached OpenRouter pricing data, trying
    various name normalizations. Returns (0.0, 0.0) if not found.
    """
    cache = _ensure_cache()
    models = cache.get("models", {})

    candidates = _normalize_model_name(model)
    for candidate in candidates:
        if candidate in models:
            entry = models[candidate]
            return (entry["input"], entry["output"])

    # Prefix matching — candidate is a prefix of a model_id (e.g. "google/gemini-2.0-flash"
    # matches "google/gemini-2.0-flash-001"), or model_id ends with the candidate after a slash
    for candidate in candidates:
        for model_id, entry in models.items():
            if model_id.startswith(candidate + "-") or model_id.startswith(candidate + ":"):
                return (entry["input"], entry["output"])

    if not model.startswith("openrouter/"):
        for candidate in candidates:
            bare_candidate = candidate.split("/", 1)[1] if "/" in candidate else candidate
            if bare_candidate in _BOOTSTRAP_PRICING:
                return _BOOTSTRAP_PRICING[bare_candidate]

    logger.warning(f"No pricing found for model '{model}'")
    return (0.0, 0.0)


def get_all_models() -> Dict[str, Dict]:
    """Get all cached model data. Returns {model_id: {input, output, name, context_length}}."""
    cache = _ensure_cache()
    return cache.get("models", {})


def get_openai_batch_models() -> Dict[str, Dict]:
    """Get the cached OpenAI batch catalog keyed by canonical model id."""
    cache = _ensure_openai_batch_cache()
    return cache.get("models", {})


def get_openai_batch_model_info(model: str) -> Optional[Dict]:
    """Return metadata for a direct OpenAI model supported in batch mode."""
    if not model:
        return None

    normalized = canonicalize_model_name(model)
    if normalized.startswith("openrouter/") or normalized.startswith("anthropic/") or normalized.startswith("google/"):
        return None

    bare = normalized.removeprefix("openai/")
    models = get_openai_batch_models()

    if bare in models:
        return models[bare]

    for entry in models.values():
        if bare in entry.get("snapshots", []):
            return entry

    return None


def get_openai_batch_price(model: str) -> Tuple[float, float]:
    """Get OpenAI batch pricing for a direct OpenAI model."""
    entry = get_openai_batch_model_info(model)
    if not entry:
        return (0.0, 0.0)
    return (entry["batch_input"], entry["batch_output"])


def refresh_openai_batch_catalog() -> int:
    """Force refresh of the OpenAI batch catalog from official docs."""
    global _openai_batch_cache
    _openai_batch_cache = None

    try:
        if OPENAI_BATCH_CACHE_FILE.exists():
            OPENAI_BATCH_CACHE_FILE.unlink()
    except OSError:
        pass

    data = _fetch_openai_batch_catalog_from_docs()
    if data:
        _openai_batch_cache = data
        return len(data.get("models", {}))

    logger.error("Failed to refresh OpenAI batch catalog")
    return 0


def get_openai_batch_cache_info() -> Dict:
    """Get info about the OpenAI batch catalog cache."""
    cache = _ensure_openai_batch_cache()
    cached_at = cache.get("cached_at", 0)
    is_bootstrap = cache.get("is_bootstrap", False)
    age_hours = (time.time() - cached_at) / 3600 if cached_at else None

    return {
        "model_count": len(cache.get("models", {})),
        "cached_at": cached_at,
        "age_hours": round(age_hours, 1) if age_hours else None,
        "is_bootstrap": is_bootstrap,
        "cache_file": str(OPENAI_BATCH_CACHE_FILE),
        "fetched_at": cache.get("fetched_at"),
    }


def refresh_cache() -> int:
    """Force refresh the pricing cache from OpenRouter. Returns model count."""
    global _pricing_cache
    _pricing_cache = None

    # Delete stale cache so we force a fresh fetch
    try:
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
    except OSError:
        pass

    data = _fetch_from_openrouter()
    if data:
        _pricing_cache = data
        return len(data.get("models", {}))

    logger.error("Failed to refresh pricing cache")
    return 0


def get_cache_info() -> Dict:
    """Get info about the current cache state."""
    cache = _ensure_cache()
    cached_at = cache.get("cached_at", 0)
    is_bootstrap = cache.get("is_bootstrap", False)
    age_hours = (time.time() - cached_at) / 3600 if cached_at else None

    return {
        "model_count": len(cache.get("models", {})),
        "cached_at": cached_at,
        "age_hours": round(age_hours, 1) if age_hours else None,
        "is_bootstrap": is_bootstrap,
        "cache_file": str(CACHE_FILE),
    }
