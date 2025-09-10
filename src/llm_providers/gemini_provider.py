"""Gemini provider implementation with structured output support."""

import logging
import os
from typing import Dict, Any, Type, Optional, List
from pydantic import BaseModel
from google import genai

logger = logging.getLogger(__name__)

class GeminiProvider:
    """Google Gemini LLM provider."""
    
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        
        # Model context limits
        self.context_limits = {
            "gemini-1.5-flash": 1048576,  # 1M tokens
            "gemini-1.5-flash-8b": 1048576,  # 1M tokens
            "gemini-1.5-pro": 2097152,  # 2M tokens
            "gemini-2.0-flash": 1048576,  # 1M tokens
            "gemini-2.5-flash": 1048576,  # 1M tokens
            "gemini-pro": 32768,  # 32K tokens
        }
    
    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> BaseModel:
        """Generate structured output using Gemini's response_schema."""
        
        # Convert messages to Gemini format
        content = self._format_messages(messages)
        
        logger.debug(f"Gemini structured output with model: {pydantic_model.__name__}")
        logger.debug(f"Schema fields: {list(pydantic_model.model_fields.keys())}")
        
        try:
            # Generate with structured output - EXACTLY like the official example
            # But make it properly async!
            import asyncio
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model,
                contents=content,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": pydantic_model,
                    "temperature": temperature,
                    "max_output_tokens": max_tokens
                }
            )
            
            # Use the parsed response directly - EXACTLY like the official example
            if hasattr(response, 'parsed') and response.parsed:
                logger.debug(f"Gemini structured output success using .parsed")
                return response.parsed
            
            # Fallback to text parsing if needed
            if response.text:
                import json
                data = json.loads(response.text)
                result = pydantic_model(**data)
                logger.debug(f"Gemini structured output success via JSON parsing")
                return result
            
            raise ValueError("Gemini returned empty response")
            
        except Exception as e:
            logger.error(f"Gemini structured output error: {e}")
            raise
    
    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> str:
        """Generate unstructured text output."""
        content = self._format_messages(messages)
        
        import asyncio
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=self.model,
            contents=content,
            config={
                "temperature": temperature,
                "max_output_tokens": max_tokens
            }
        )
        
        return response.text
    
    def _format_messages(self, messages: List[Dict[str, str]]) -> str:
        """Convert OpenAI-style messages to Gemini format."""
        # Gemini expects a single content string for simple cases
        # Concatenate all messages with role indicators
        formatted_parts = []
        
        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            
            if role == 'system':
                # System messages become instructions
                formatted_parts.append(f"Instructions: {content}")
            elif role == 'assistant':
                # Assistant messages are previous responses
                formatted_parts.append(f"Assistant: {content}")
            else:
                # User messages
                formatted_parts.append(f"User: {content}")
        
        return "\n\n".join(formatted_parts)
    
    def count_tokens(self, text: str) -> int:
        """Count tokens for Gemini models."""
        # Gemini uses roughly 1 token per 4 characters as an approximation
        # This is not exact but provides a reasonable estimate
        return len(text) // 4
    
    @property
    def max_context_tokens(self) -> int:
        """Maximum context window size."""
        return self.context_limits.get(self.model, 1048576)  # Default to 1M