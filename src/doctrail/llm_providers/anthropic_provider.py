"""Anthropic provider implementation with structured output support."""

import json
import logging
from typing import Dict, Any, Type, Optional, List, Tuple, Union
from dataclasses import dataclass
from pydantic import BaseModel, ValidationError
from anthropic import AsyncAnthropic
from ..utils.model_pricing import canonicalize_model_name

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token usage information from LLM API calls."""
    input_tokens: int
    output_tokens: int
    model: str
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    batch_pricing: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimate_cost(self) -> float:
        """Estimate cost in USD based on model pricing."""
        from ..utils.model_pricing import get_model_price
        input_price, output_price = get_model_price(self.model)
        if input_price == 0.0 and output_price == 0.0:
            logger.warning(f"Unknown model {self.model} for pricing, cost will show as $0")

        batch_multiplier = 0.5 if self.batch_pricing else 1.0
        cache_read_tokens = min(self.cached_input_tokens or 0, self.input_tokens)
        cache_creation_tokens = min(
            self.cache_creation_input_tokens or 0,
            max(self.input_tokens - cache_read_tokens, 0),
        )
        uncached_tokens = max(self.input_tokens - cache_read_tokens - cache_creation_tokens, 0)

        input_cost = (
            (uncached_tokens / 1_000_000) * input_price
            + (cache_creation_tokens / 1_000_000) * input_price * 1.25
            + (cache_read_tokens / 1_000_000) * input_price * 0.1
        )
        output_cost = (self.output_tokens / 1_000_000) * output_price
        input_cost *= batch_multiplier
        output_cost *= batch_multiplier
        return input_cost + output_cost


class AnthropicProvider:
    """Anthropic Claude LLM provider."""

    _UNSUPPORTED_INTEGER_SCHEMA_KEYS = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }

    def __init__(self, api_key: str, model: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = canonicalize_model_name(model).removeprefix("anthropic/")

        # Model context limits
        self.context_limits = {
            "claude-opus-4": 200000,
            "claude-opus-4-6": 200000,
            "claude-sonnet-4": 200000,
            "claude-sonnet-4-5": 200000,
            "claude-sonnet-4-6": 200000,
            "claude-haiku-4-5": 200000,
            "claude-3-5-sonnet": 200000,
            "claude-3-5-haiku": 200000,
            "claude-3-opus": 200000,
            "claude-3-sonnet": 200000,
            "claude-3-haiku": 200000,
        }

    @staticmethod
    def _usage_attr(value: Any, attr: str, default: Any = None) -> Any:
        """Read an attribute from either a dict or an SDK object."""
        if isinstance(value, dict):
            return value.get(attr, default)
        return getattr(value, attr, default)

    @classmethod
    def _usage_int(cls, value: Any, attr: str) -> int:
        """Read an integer token counter from a provider usage payload."""
        raw_value = cls._usage_attr(value, attr, 0)
        if raw_value is None:
            return 0
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, float):
            return int(raw_value)
        if isinstance(raw_value, str):
            try:
                return int(raw_value)
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _content_attr(value: Any, attr: str, default: Any = None) -> Any:
        """Read a content-block attribute from either a dict or an SDK object."""
        if isinstance(value, dict):
            return value.get(attr, default)
        return getattr(value, attr, default)

    @staticmethod
    def _cache_control() -> Dict[str, str]:
        """Return a fresh Anthropic ephemeral cache-control marker."""
        return {"type": "ephemeral"}

    @classmethod
    def _text_block(cls, text: str, *, cache: bool = False) -> Dict[str, Any]:
        """Build an Anthropic text block, optionally marking it as a cache breakpoint."""
        block: Dict[str, Any] = {"type": "text", "text": text}
        if cache and text:
            block["cache_control"] = cls._cache_control()
        return block

    @classmethod
    def _content_has_cache_control(cls, content: Any) -> bool:
        """Return whether a message content payload already has a cache breakpoint."""
        if not isinstance(content, list):
            return False
        return any(isinstance(block, dict) and block.get("cache_control") for block in content)

    @classmethod
    def _cached_content(cls, content: Any) -> Any:
        """Ensure a message content payload has a cache breakpoint when possible."""
        if isinstance(content, str):
            return [cls._text_block(content, cache=True)] if content else content
        if not isinstance(content, list):
            return content
        blocks = [dict(block) if isinstance(block, dict) else block for block in content]
        if cls._content_has_cache_control(blocks):
            return blocks
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                block["cache_control"] = cls._cache_control()
                break
        return blocks

    @classmethod
    def _append_system_text(
        cls,
        system_prompt: Optional[Union[str, List[Dict[str, Any]]]],
        text: str,
    ) -> Union[str, List[Dict[str, Any]]]:
        """Append an instruction to a system prompt represented as text or text blocks."""
        if not system_prompt:
            return [cls._text_block(text, cache=True)]
        if isinstance(system_prompt, str):
            return [
                cls._text_block(system_prompt, cache=True),
                cls._text_block(text),
            ]
        blocks = [dict(block) if isinstance(block, dict) else block for block in system_prompt]
        blocks.append(cls._text_block(text))
        return blocks

    @staticmethod
    def _add_additional_properties_false(schema: Dict[str, Any]) -> None:
        """Recursively mark object schemas as closed for structured output."""
        if not isinstance(schema, dict):
            return

        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
            for sub_schema in (schema.get("properties") or {}).values():
                AnthropicProvider._add_additional_properties_false(sub_schema)

        array_items = schema.get("items")
        if isinstance(array_items, dict):
            AnthropicProvider._add_additional_properties_false(array_items)

        for key in ("anyOf", "allOf", "oneOf"):
            for sub_schema in schema.get(key) or []:
                AnthropicProvider._add_additional_properties_false(sub_schema)

        defs = schema.get("$defs") or {}
        for sub_schema in defs.values():
            AnthropicProvider._add_additional_properties_false(sub_schema)

    @classmethod
    def _strip_unsupported_integer_constraints(
        cls,
        schema: Dict[str, Any],
        *,
        path: str = "$",
        removed: Optional[List[str]] = None,
    ) -> List[str]:
        """Remove JSON Schema integer constraints Anthropic batch rejects."""
        if removed is None:
            removed = []
        if not isinstance(schema, dict):
            return removed

        schema_type = schema.get("type")
        if schema_type == "integer":
            for key in cls._UNSUPPORTED_INTEGER_SCHEMA_KEYS:
                if key in schema:
                    schema.pop(key, None)
                    removed.append(f"{path}.{key}")

        properties = schema.get("properties") or {}
        for name, sub_schema in properties.items():
            cls._strip_unsupported_integer_constraints(
                sub_schema,
                path=f"{path}.properties.{name}",
                removed=removed,
            )

        array_items = schema.get("items")
        if isinstance(array_items, dict):
            cls._strip_unsupported_integer_constraints(
                array_items,
                path=f"{path}.items",
                removed=removed,
            )

        for key in ("anyOf", "allOf", "oneOf"):
            for index, sub_schema in enumerate(schema.get(key) or []):
                cls._strip_unsupported_integer_constraints(
                    sub_schema,
                    path=f"{path}.{key}[{index}]",
                    removed=removed,
                )

        defs = schema.get("$defs") or {}
        for name, sub_schema in defs.items():
            cls._strip_unsupported_integer_constraints(
                sub_schema,
                path=f"{path}.$defs.{name}",
                removed=removed,
            )

        return removed

    @classmethod
    def get_batch_schema_compatibility_issues(
        cls,
        pydantic_model: Type[BaseModel],
    ) -> List[Dict[str, Any]]:
        """Return client-side compatibility warnings for Anthropic batch structured output."""
        schema = json.loads(json.dumps(pydantic_model.model_json_schema()))
        removed = cls._strip_unsupported_integer_constraints(schema)
        if not removed:
            return []

        return [{
            "level": "warning",
            "code": "anthropic_batch_unsupported_integer_constraints",
            "paths": removed,
            "message": (
                "Anthropic batch structured output does not support integer bounds or multipleOf. "
                "Doctrail will strip these constraints before submission: "
                + ", ".join(removed)
            ),
        }]

    @classmethod
    def _schema_for_structured_output(cls, pydantic_model: Type[BaseModel]) -> Dict[str, Any]:
        """Return a Claude-compatible JSON schema for structured output."""
        schema = json.loads(json.dumps(pydantic_model.model_json_schema()))
        cls._add_additional_properties_false(schema)
        cls._strip_unsupported_integer_constraints(schema)
        return schema

    def supports_batch(self) -> bool:
        """Return whether this provider can use Anthropic's native batch API."""
        return True

    def build_token_usage(
        self,
        usage_payload: Optional[Dict[str, Any]],
        *,
        batch_pricing: bool = False,
    ) -> Optional[TokenUsage]:
        """Normalize token usage payloads from Anthropic messages and batch results."""
        if not usage_payload:
            return None

        input_tokens = self._usage_int(usage_payload, "input_tokens")
        cache_creation_input_tokens = self._usage_int(usage_payload, "cache_creation_input_tokens")
        cache_read_input_tokens = self._usage_int(usage_payload, "cache_read_input_tokens")
        output_tokens = self._usage_int(usage_payload, "output_tokens")

        # Anthropic bills total input as input + cache creation + cache read.
        total_input_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
        return TokenUsage(
            input_tokens=total_input_tokens,
            output_tokens=output_tokens,
            model=self.model,
            cached_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            batch_pricing=batch_pricing,
        )

    def build_batch_message_request(
        self,
        messages: List[Dict[str, Any]],
        pydantic_model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build one raw /v1/messages request payload for Anthropic batch submission."""
        system_prompt, api_messages = self._convert_messages(messages)
        request_body: Dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
        }
        if system_prompt:
            request_body["system"] = system_prompt
        if pydantic_model is not None:
            request_body["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": self._schema_for_structured_output(pydantic_model),
                }
            }
        return request_body

    @classmethod
    def extract_message_text(cls, message_payload: Dict[str, Any]) -> str:
        """Extract assistant text content from an Anthropic message payload."""
        content = cls._usage_attr(message_payload, "content") or []
        if isinstance(content, str):
            return content

        text_parts: List[str] = []
        for block in content:
            if cls._content_attr(block, "type") == "text":
                text_parts.append(cls._content_attr(block, "text", "") or "")

        if text_parts:
            return "".join(text_parts)
        raise ValueError(f"Anthropic message response did not contain text content: {message_payload}")

    @staticmethod
    def _validation_error_is_null_only(exc: ValidationError) -> bool:
        """Return True when every validation error was caused by a null input."""
        errors = exc.errors()
        return bool(errors) and all(error.get("input") is None for error in errors)

    def parse_batch_message_response(
        self,
        message_payload: Dict[str, Any],
        pydantic_model: Optional[Type[BaseModel]] = None,
        nullable_pydantic_model: Optional[Type[BaseModel]] = None,
    ) -> Tuple[Any, Optional[TokenUsage], str]:
        """Normalize one Anthropic batch message result into Doctrail's storage contract."""
        text = self.extract_message_text(message_payload)
        usage_payload = self._usage_attr(message_payload, "usage") or {}
        usage = self.build_token_usage(usage_payload, batch_pricing=True)

        if pydantic_model is None:
            return text, usage, json.dumps({"result": text}, ensure_ascii=False)

        data = self._extract_json_from_text(text)
        try:
            parsed = pydantic_model(**data)
        except ValidationError as exc:
            if nullable_pydantic_model is None or not self._validation_error_is_null_only(exc):
                raise
            parsed = nullable_pydantic_model(**data)
        return parsed.model_dump(mode="json"), usage, json.dumps(data, ensure_ascii=False)

    async def generate_structured(
        self,
        messages: List[Dict[str, Any]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        return_usage: bool = False
    ) -> Union[BaseModel, Tuple[BaseModel, Optional[TokenUsage]]]:
        """Generate structured output using Anthropic's messages.parse().

        Uses the native structured output support via output_format parameter.
        Falls back to text + JSON extraction if parse fails.
        """
        logger.debug(f"Anthropic structured output with model: {pydantic_model.__name__}")
        logger.debug(f"Schema fields: {list(pydantic_model.model_fields.keys())}")

        system_prompt, api_messages = self._convert_messages(messages)
        usage = None

        # Tier 1: Native structured output via messages.parse()
        try:
            kwargs = {
                "model": self.model,
                "messages": api_messages,
                "max_tokens": max_tokens or 4096,
                "output_format": pydantic_model,
                "temperature": temperature,
            }
            if system_prompt:
                kwargs["system"] = system_prompt

            response = await self.client.messages.parse(**kwargs)

            if hasattr(response, 'usage') and response.usage:
                usage = self.build_token_usage(response.usage)
                logger.debug(f"Token usage: {usage.input_tokens} in, {usage.output_tokens} out, est. ${usage.estimate_cost():.4f}")

            parsed_result = response.parsed_output
            if parsed_result is None:
                raise ValueError("Structured output returned None")

            logger.debug(f"Anthropic structured output success: {type(parsed_result)}")
            if return_usage:
                return parsed_result, usage
            return parsed_result

        except Exception as e:
            logger.warning(f"Anthropic structured output (tier 1) failed: {e}")

        # Tier 2: Text generation + JSON extraction fallback
        try:
            schema = pydantic_model.model_json_schema()
            schema_instruction = (
                "You MUST respond with ONLY a valid JSON object matching this schema. "
                "No markdown, no explanation, no text before or after the JSON.\n"
                f"Schema: {json.dumps(schema)}"
            )

            kwargs = {
                "model": self.model,
                "messages": api_messages,
                "max_tokens": max_tokens or 4096,
                "system": self._append_system_text(system_prompt, schema_instruction),
                "temperature": temperature,
            }

            response = await self.client.messages.create(**kwargs)

            if hasattr(response, 'usage') and response.usage:
                usage = self.build_token_usage(response.usage)

            text = response.content[0].text
            data = self._extract_json_from_text(text)
            result = pydantic_model(**data)
            logger.debug(f"Anthropic text + JSON extraction success: {type(result)}")
            if return_usage:
                return result, usage
            return result

        except Exception as e:
            logger.error(f"All tiers failed for Anthropic model {self.model}. Error: {e}")
            raise

    async def generate_text(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None
    ) -> str:
        """Generate unstructured text output."""
        system_prompt, api_messages = self._convert_messages(messages)

        kwargs = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await self.client.messages.create(**kwargs)
        return response.content[0].text

    def _convert_messages(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[Optional[Union[str, List[Dict[str, Any]]]], List[Dict[str, Any]]]:
        """Convert OpenAI-style messages to Anthropic format.

        Anthropic API takes system messages as a top-level 'system' parameter,
        not in the messages list.

        Returns:
            Tuple of (system_prompt or None, filtered messages list)
        """
        system_parts: List[str] = []
        api_messages = []

        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            if role == 'system':
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    system_parts.extend(
                        str(block.get("text", ""))
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
            else:
                api_messages.append({"role": role, "content": content})

        system_prompt = None
        if system_parts:
            system_text = "\n\n".join(part for part in system_parts if part)
            if system_text:
                system_prompt = [self._text_block(system_text, cache=True)]

        if not any(self._content_has_cache_control(msg.get("content")) for msg in api_messages):
            for msg in api_messages:
                msg["content"] = self._cached_content(msg.get("content", ""))
                if self._content_has_cache_control(msg["content"]):
                    break

        return system_prompt, api_messages

    @staticmethod
    def _extract_json_from_text(text: str) -> dict:
        """Extract a JSON object from text that may contain markdown, preamble, etc."""
        import re

        if not text or not text.strip():
            raise ValueError("Empty text, cannot extract JSON")

        # Strategy 1: raw text
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # Strategy 2: markdown code blocks
        code_block_pattern = r'```(?:json)?\s*\n?(.*?)\n?\s*```'
        matches = re.findall(code_block_pattern, text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

        # Strategy 3: first { to last }
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace:last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from response: {text[:300]}")

    def count_tokens(self, text: str) -> int:
        """Count tokens (approximation — Anthropic uses ~1 token per 4 chars)."""
        return len(text) // 4

    @property
    def max_context_tokens(self) -> int:
        """Maximum context window size."""
        return self.context_limits.get(self.model, 200000)
