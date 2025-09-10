"""OpenAI provider implementation."""

import logging
from typing import Dict, Any, Type, Optional, List
from pydantic import BaseModel
from openai import AsyncOpenAI

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logging.warning("tiktoken not available - token counting will use approximation")

logger = logging.getLogger(__name__)

class OpenAIProvider:
    """OpenAI LLM provider."""
    
    def __init__(self, api_key: str, model: str):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.encoding = None
        
        # Model context limits
        self.context_limits = {
            "gpt-4": 8192,
            "gpt-4-32k": 32768,
            "gpt-4-turbo": 128000,
            "gpt-4-turbo-preview": 128000,
            "gpt-4o": 128000,
            "gpt-4o-mini": 128000,
            "gpt-3.5-turbo": 16385,
            "gpt-3.5-turbo-16k": 16385,
        }
    
    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> BaseModel:
        """Generate structured output using OpenAI's response_format."""
        
        # Debug: Check what we're sending for structured output
        logger.debug(f"OpenAI structured output with model: {pydantic_model.__name__}")
        logger.debug(f"Schema fields: {list(pydantic_model.model_fields.keys())}")
        
        try:
            response = await self.client.beta.chat.completions.parse(
                model=self.model,
                messages=messages,
                response_format=pydantic_model,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            parsed_result = response.choices[0].message.parsed
            
            if parsed_result is None:
                logger.error("OpenAI returned None for structured output")
                raise ValueError("OpenAI structured output returned None")
            
            logger.debug(f"OpenAI structured output success: {type(parsed_result)}")
            return parsed_result
            
        except Exception as e:
            logger.error(f"OpenAI structured output error: {e}")
            # Fall back to text generation + manual parsing
            text_response = await self.generate_text(messages, temperature, max_tokens)
            
            try:
                # Try to parse as JSON
                import json
                data = json.loads(text_response)
                return pydantic_model(**data)
            except:
                # Last resort - create empty model
                logger.error(f"Failed to parse OpenAI response as JSON: {text_response[:200]}")
                raise
    
    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> str:
        """Generate unstructured text output."""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content
    
    def count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken or fallback to approximation."""
        if not TIKTOKEN_AVAILABLE:
            # Fallback: approximately 4 characters per token
            return len(text) // 4
            
        if self.encoding is None:
            try:
                # Try to get the exact encoding for the model
                self.encoding = tiktoken.encoding_for_model(self.model)
            except KeyError:
                # Default to cl100k_base for newer models
                self.encoding = tiktoken.get_encoding("cl100k_base")
        
        return len(self.encoding.encode(text))
    
    @property
    def max_context_tokens(self) -> int:
        """Maximum context window size."""
        return self.context_limits.get(self.model, 8192)