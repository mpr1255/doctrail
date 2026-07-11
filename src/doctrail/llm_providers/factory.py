"""Factory for creating LLM providers."""

import os
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Optional, Union
from ..core_utils import load_doctrail_environment
from ..utils.model_pricing import canonicalize_model_name
from .openai_provider import OpenAIProvider
from .gemini_provider import GeminiProvider
from .anthropic_provider import AnthropicProvider
from .claude_sdk_provider import ClaudeSDKProvider
from .cli_provider import CLIProvider
from .replay_provider import ReplayProvider

logger = logging.getLogger(__name__)

# Module-level cache: populated once per process from OpenRouter's /api/v1/models
_openrouter_capabilities_cache: Dict[str, Dict[str, bool]] = {}
_openrouter_cache_loaded = False


def _get_openrouter_model_capabilities(
    model: str, api_key: str
) -> Optional[Dict[str, bool]]:
    """Fetch structured output capabilities for an OpenRouter model.

    Queries GET https://openrouter.ai/api/v1/models and caches all results.
    Returns {"structured_outputs": bool, "response_format": bool} or None on error.
    """
    global _openrouter_capabilities_cache, _openrouter_cache_loaded

    # Return from cache if already loaded
    if _openrouter_cache_loaded:
        caps = _openrouter_capabilities_cache.get(model)
        if caps is None:
            logger.warning(f"Model '{model}' not found in OpenRouter models list")
        return caps

    # Fetch all models from OpenRouter
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        for entry in data.get("data", []):
            model_id = entry.get("id", "")
            supported = entry.get("supported_parameters", [])
            _openrouter_capabilities_cache[model_id] = {
                "structured_outputs": "structured_outputs" in supported,
                "response_format": "response_format" in supported,
            }

        _openrouter_cache_loaded = True
        logger.debug(
            f"Cached capabilities for {len(_openrouter_capabilities_cache)} OpenRouter models"
        )

        caps = _openrouter_capabilities_cache.get(model)
        if caps is None:
            logger.warning(f"Model '{model}' not found in OpenRouter models list")
        return caps

    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to fetch OpenRouter model capabilities: {e}")
        return None

def get_llm_provider(
    model: str,
    *,
    enrichment_name: Optional[str] = None,
) -> Union[OpenAIProvider, GeminiProvider, AnthropicProvider, CLIProvider, ReplayProvider]:
    """Get the appropriate LLM provider for a model."""
    load_doctrail_environment()
    model = canonicalize_model_name(model)

    if model == "replay" or model.startswith("replay/"):
        logger.debug(f"Creating replay provider for model: {model}")
        return ReplayProvider(model=model, enrichment_name=enrichment_name)

    # CLI providers: cli/<tool>/<model>
    # Claude uses the Agent SDK; gemini/codex still use raw subprocess (CLIProvider)
    if model.startswith('cli/'):
        parts = model.removeprefix('cli/').split('/', 1)
        cli_tool = parts[0]
        cli_model = parts[1] if len(parts) > 1 else None

        if cli_tool == "claude":
            logger.debug(f"Creating Claude SDK provider: model={cli_model}")
            return ClaudeSDKProvider(model=cli_model)
        else:
            logger.debug(f"Creating CLI provider: tool={cli_tool}, model={cli_model}")
            return CLIProvider(cli_tool=cli_tool, model=cli_model)

    # OpenRouter: openrouter/<provider>/<model> → OpenAI-compatible with different base_url
    if model.startswith('openrouter/'):
        actual_model = model.removeprefix('openrouter/')
        api_key = os.environ.get('OPENROUTER_API_KEY')
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is required for OpenRouter models")

        try:
            capabilities = _get_openrouter_model_capabilities(actual_model, api_key)
        except Exception:
            capabilities = None  # API down → don't block, use full fallback chain

        logger.debug(f"Creating OpenRouter provider for model: {actual_model} (capabilities: {capabilities})")
        return OpenAIProvider(
            api_key=api_key,
            model=actual_model,
            base_url="https://openrouter.ai/api/v1",
            capabilities=capabilities,
        )

    # Self-hosted OpenAI-compatible servers such as vLLM and Ollama.
    if model.startswith('openai-compatible/'):
        actual_model = model.removeprefix('openai-compatible/')
        base_url = os.environ.get('OPENAI_COMPATIBLE_BASE_URL')
        if not base_url:
            raise ValueError(
                "OPENAI_COMPATIBLE_BASE_URL environment variable is required "
                "for openai-compatible models"
            )
        api_key = os.environ.get('OPENAI_COMPATIBLE_API_KEY', 'not-required')

        logger.debug(
            "Creating OpenAI-compatible provider for model %s at %s",
            actual_model,
            base_url,
        )
        return OpenAIProvider(
            api_key=api_key,
            model=actual_model,
            base_url=base_url,
            capabilities={"structured_outputs": True, "response_format": False},
            free_inference=True,
        )

    # Anthropic/Claude: claude-* prefix or anthropic/ prefix (direct API)
    if model.startswith('claude') or model.startswith('anthropic/'):
        actual_model = model.removeprefix('anthropic/')
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required for Anthropic/Claude models")

        logger.debug(f"Creating Anthropic provider for model: {actual_model}")
        return AnthropicProvider(api_key=api_key, model=actual_model)

    # Determine provider based on model name
    if 'gemini' in model.lower():
        # Gemini model
        api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY environment variable is required for Gemini models")
        
        logger.debug(f"Creating Gemini provider for model: {model}")
        return GeminiProvider(api_key=api_key, model=model)
    
    else:
        # Default to OpenAI (includes gpt, claude via openai-compatible endpoints, etc.)
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required for OpenAI models")
        
        logger.debug(f"Creating OpenAI provider for model: {model}")
        return OpenAIProvider(api_key=api_key, model=model)

def is_gemini_model(model: str) -> bool:
    """Check if a model is a Gemini model."""
    return 'gemini' in model.lower()
