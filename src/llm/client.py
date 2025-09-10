"""LLM client for handling API calls to language models."""

import logging
from typing import List, Dict, Optional, Type, Any
from pydantic import BaseModel

from ..constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from ..utils.logging_config import suppress_noisy_loggers

# Import providers
try:
    suppress_noisy_loggers()
    from ..llm_providers.factory import get_llm_provider
    PROVIDERS_AVAILABLE = True
except ImportError:
    PROVIDERS_AVAILABLE = False
    logging.warning("LLM providers not available")


class LLMClient:
    """Client for interacting with language models."""
    
    def __init__(self, model: str, config: Optional[Dict[str, Any]] = None):
        """Initialize LLM client.
        
        Args:
            model: Model name
            config: Optional configuration with model settings
        """
        self.model = model
        self.config = config or {}
        self.logger = logging.getLogger(__name__)
        self._provider = None
        
    async def call(
        self, 
        messages: List[Dict[str, str]], 
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        verbose: bool = False
    ) -> str:
        """Call LLM with messages.
        
        Args:
            messages: List of message dictionaries
            system_prompt: Optional system prompt
            temperature: Optional temperature override
            max_tokens: Optional max tokens override
            verbose: Enable verbose output
            
        Returns:
            LLM response text
        """
        if not PROVIDERS_AVAILABLE:
            raise RuntimeError("LLM providers not available")
            
        # Get provider
        if not self._provider:
            self._provider = get_llm_provider(self.model, self.config)
        
        # Build full messages list
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        
        # Get model config
        model_config = self._get_model_config(self.model)
        temp = temperature if temperature is not None else model_config.get('temperature', DEFAULT_TEMPERATURE)
        max_tok = max_tokens if max_tokens is not None else model_config.get('max_tokens', DEFAULT_MAX_TOKENS)
        
        if verbose:
            self.logger.info(f"Calling {self.model} with {len(messages)} messages")
            
        # Make API call through provider
        response = await self._provider.generate(
            messages=full_messages,
            temperature=temp,
            max_tokens=max_tok
        )
        
        return response
    
    async def call_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        verbose: bool = False
    ) -> BaseModel:
        """Call LLM with structured output.
        
        Args:
            messages: List of message dictionaries
            pydantic_model: Pydantic model for structured output
            system_prompt: Optional system prompt
            temperature: Optional temperature override
            max_tokens: Optional max tokens override
            verbose: Enable verbose output
            
        Returns:
            Parsed Pydantic model instance
        """
        if not PROVIDERS_AVAILABLE:
            raise RuntimeError("LLM providers not available")
            
        # Get provider
        if not self._provider:
            self._provider = get_llm_provider(self.model, self.config)
            
        # Check if provider supports structured output
        if not hasattr(self._provider, 'generate_structured'):
            raise ValueError(f"Provider for {self.model} doesn't support structured output")
        
        # Build full messages list
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        
        # Get model config
        model_config = self._get_model_config(self.model)
        temp = temperature if temperature is not None else model_config.get('temperature', DEFAULT_TEMPERATURE)
        max_tok = max_tokens if max_tokens is not None else model_config.get('max_tokens', DEFAULT_MAX_TOKENS)
        
        if verbose:
            self.logger.info(f"Calling {self.model} with structured output: {pydantic_model.__name__}")
            
        # Make API call through provider
        response = await self._provider.generate_structured(
            messages=full_messages,
            response_model=pydantic_model,
            temperature=temp,
            max_tokens=max_tok
        )
        
        return response
    
    def _get_model_config(self, model: str) -> Dict[str, Any]:
        """Get model configuration from config."""
        if 'models' in self.config and model in self.config['models']:
            return self.config['models'][model]
        return {}
    
    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost for token usage.
        
        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            
        Returns:
            Estimated cost in dollars
        """
        # This would use pricing data from the provider
        # For now, return a placeholder
        return 0.0