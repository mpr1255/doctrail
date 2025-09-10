"""LLM provider implementations."""

from typing import Protocol, Dict, Any, Type, Optional, List
from pydantic import BaseModel

class LLMProvider(Protocol):
    """Protocol for LLM providers."""
    
    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> BaseModel:
        """Generate structured output using a Pydantic model."""
        ...
    
    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> str:
        """Generate unstructured text output."""
        ...
    
    def count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        ...
    
    @property
    def max_context_tokens(self) -> int:
        """Maximum context window size."""
        ...

# Import concrete implementations
from .factory import get_llm_provider, is_gemini_model
from .openai_provider import OpenAIProvider
from .gemini_provider import GeminiProvider

__all__ = ['LLMProvider', 'get_llm_provider', 'is_gemini_model', 'OpenAIProvider', 'GeminiProvider']