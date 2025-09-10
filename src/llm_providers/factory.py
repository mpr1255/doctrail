"""Factory for creating LLM providers."""

import os
import logging
from typing import Union
from .openai_provider import OpenAIProvider
from .gemini_provider import GeminiProvider

logger = logging.getLogger(__name__)

def get_llm_provider(model: str) -> Union[OpenAIProvider, GeminiProvider]:
    """Get the appropriate LLM provider for a model."""
    
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