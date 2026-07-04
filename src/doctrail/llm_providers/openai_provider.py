"""OpenAI provider implementation."""

import json
import logging
import re
from typing import Dict, Any, Type, Optional, List, Tuple, Union
from dataclasses import dataclass
from pydantic import BaseModel, ValidationError
from openai import AsyncOpenAI

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logging.warning("tiktoken not available - token counting will use approximation")

logger = logging.getLogger(__name__)
OPENAI_REASONING_EFFORT_VALUES = {"minimal", "low", "medium", "high"}


@dataclass
class TokenUsage:
    """Token usage information from LLM API calls."""
    input_tokens: int
    output_tokens: int
    model: str
    cached_input_tokens: int = 0
    batch_pricing: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimate_cost(self) -> float:
        """Estimate cost in USD based on model pricing."""
        from ..utils.model_pricing import get_model_price, get_openai_batch_model_info

        if self.model == "replay" or self.model.startswith("replay/"):
            return 0.0

        if self.batch_pricing:
            batch_entry = get_openai_batch_model_info(self.model)
            if batch_entry:
                cached_tokens = min(self.cached_input_tokens or 0, self.input_tokens)
                uncached_tokens = max(self.input_tokens - cached_tokens, 0)
                uncached_input_cost = (uncached_tokens / 1_000_000) * batch_entry["batch_input"]
                cached_input_price = batch_entry.get("batch_cached_input")
                if cached_input_price is None:
                    cached_input_price = batch_entry["batch_input"]
                cached_input_cost = (cached_tokens / 1_000_000) * cached_input_price
                output_cost = (self.output_tokens / 1_000_000) * batch_entry["batch_output"]
                return uncached_input_cost + cached_input_cost + output_cost

        input_price, output_price = get_model_price(self.model)
        if input_price == 0.0 and output_price == 0.0:
            logger.warning(f"Unknown model {self.model} for pricing, cost will show as $0")
        input_cost = (self.input_tokens / 1_000_000) * input_price
        output_cost = (self.output_tokens / 1_000_000) * output_price
        return input_cost + output_cost


class StructuredGenerationError(Exception):
    """Structured generation failure that may still carry billed token usage."""

    def __init__(self, message: str, usage: Optional[TokenUsage] = None):
        super().__init__(message)
        self.usage = usage


def _combine_token_usage(usages: List[Optional[TokenUsage]]) -> Optional[TokenUsage]:
    """Combine usage from multiple billed fallback attempts."""
    present = [usage for usage in usages if usage is not None]
    if not present:
        return None
    return TokenUsage(
        input_tokens=sum(usage.input_tokens for usage in present),
        output_tokens=sum(usage.output_tokens for usage in present),
        cached_input_tokens=sum(usage.cached_input_tokens for usage in present),
        model=present[-1].model,
        batch_pricing=any(usage.batch_pricing for usage in present),
    )


class OpenAIProvider:
    """OpenAI LLM provider. Also used for OpenAI-compatible APIs (e.g. OpenRouter)."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, capabilities: Optional[Dict[str, bool]] = None):
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**client_kwargs)
        self.model = model
        self.encoding = None
        self._is_third_party = base_url is not None  # OpenRouter, etc.
        self._capabilities = capabilities  # None = native OpenAI or API unavailable

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

    def supports_reasoning_effort(self) -> bool:
        """Return whether this provider/model supports OpenAI reasoning effort."""
        return not self._is_third_party and self.model.startswith("gpt-5")

    def _resolve_reasoning_effort(self, reasoning_effort: Optional[str]) -> Optional[str]:
        """Normalize reasoning_effort and apply a safe GPT-5 default."""
        if not self.supports_reasoning_effort():
            return None

        if reasoning_effort is None:
            return "minimal"

        normalized = str(reasoning_effort).strip().lower()
        if normalized not in OPENAI_REASONING_EFFORT_VALUES:
            raise ValueError(
                f"Invalid reasoning_effort '{reasoning_effort}'. "
                f"Expected one of: {', '.join(sorted(OPENAI_REASONING_EFFORT_VALUES))}"
            )
        return normalized

    def _max_tokens_kwargs(self, max_tokens: Optional[int]) -> Dict[str, Any]:
        """Return the correct token-limit parameter for the target model."""
        if max_tokens is None:
            return {}
        if self.model.startswith("gpt-5") and not self._is_third_party:
            return {"max_completion_tokens": max_tokens}
        return {"max_tokens": max_tokens}
    
    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        return_usage: bool = False,
        reasoning_effort: Optional[str] = None,
    ) -> Union[BaseModel, Tuple[BaseModel, Optional[TokenUsage]]]:
        """Generate structured output with three-tier fallback.

        Fallback chain:
        1. Native structured output (beta.chat.completions.parse) — OpenAI-specific
        2. JSON mode (response_format=json_object + schema in prompt) — widely supported
        3. Plain text generation + JSON extraction from response — universal

        Args:
            messages: List of message dicts with role/content
            pydantic_model: The Pydantic model class for structured output
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            return_usage: If True, return (result, TokenUsage) tuple

        Returns:
            If return_usage=False: The parsed Pydantic model
            If return_usage=True: Tuple of (parsed model, TokenUsage or None)
        """

        logger.debug(f"OpenAI structured output with model: {pydantic_model.__name__}")
        logger.debug(f"Schema fields: {list(pydantic_model.model_fields.keys())}")

        caps = self._capabilities
        usage = None
        billed_usages: List[Optional[TokenUsage]] = []
        resolved_reasoning_effort = self._resolve_reasoning_effort(reasoning_effort)

        # Models with neither structured_outputs nor response_format: skip tiers 1&2,
        # fall through to tier 3 (text + JSON extraction) which works universally
        no_structured_support = (
            caps is not None
            and not caps.get("structured_outputs")
            and not caps.get("response_format")
        )

        # --- Tier 1: Native structured output ---
        # Skip if we know the model doesn't support structured_outputs
        if not no_structured_support and (caps is None or caps.get("structured_outputs")):
            try:
                if self.model.startswith("gpt-5"):
                    temperature = 1.0

                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "response_format": pydantic_model,
                    "temperature": temperature
                }
                if resolved_reasoning_effort is not None:
                    kwargs["reasoning_effort"] = resolved_reasoning_effort
                kwargs.update(self._max_tokens_kwargs(max_tokens))

                response = await self.client.beta.chat.completions.parse(**kwargs)

                if hasattr(response, 'usage') and response.usage:
                    usage = TokenUsage(
                        input_tokens=response.usage.prompt_tokens,
                        output_tokens=response.usage.completion_tokens,
                        model=self.model
                    )
                    billed_usages.append(usage)
                    logger.debug(f"Token usage: {usage.input_tokens} in, {usage.output_tokens} out, est. ${usage.estimate_cost():.4f}")

                parsed_result = response.choices[0].message.parsed

                if parsed_result is None:
                    raise ValueError("Structured output returned None")

                logger.debug(f"Tier 1 (native structured) success: {type(parsed_result)}")
                if return_usage:
                    return parsed_result, _combine_token_usage(billed_usages)
                return parsed_result

            except Exception as e:
                if getattr(e, "usage", None):
                    billed_usages.append(e.usage)
                logger.warning(f"Tier 1 (native structured output) failed: {e}")
        else:
            logger.debug(f"Tier 1 skipped: model '{self.model}' lacks structured_outputs support")

        # --- Tier 2: JSON mode with schema instructions in prompt ---
        # Skip if we know the model doesn't support response_format
        if not no_structured_support and (caps is None or caps.get("response_format")):
            try:
                result, usage = await self._generate_json_mode(
                    messages,
                    pydantic_model,
                    temperature,
                    max_tokens,
                    reasoning_effort=resolved_reasoning_effort,
                )
                billed_usages.append(usage)
                logger.debug(f"Tier 2 (JSON mode) success: {type(result)}")
                if return_usage:
                    return result, _combine_token_usage(billed_usages)
                return result

            except Exception as e:
                if getattr(e, "usage", None):
                    billed_usages.append(e.usage)
                logger.warning(f"Tier 2 (JSON mode) failed: {e}")
        else:
            logger.debug(f"Tier 2 skipped: model '{self.model}' lacks response_format support")

        # --- Tier 3: Plain text + JSON extraction ---
        # Universal fallback — works for any model that can generate text.
        # Used by: native OpenAI (caps=None) as emergency, OpenRouter models
        # with no structured support (skip tiers 1&2), and any model where
        # tiers 1&2 failed.
        if caps is not None and not no_structured_support:
            # Models that *should* support tiers 1 or 2 but both failed
            raise ValueError(
                f"Structured output failed for model '{self.model}'. "
                f"Capabilities: {caps}. Consider using a model with broader support."
            )

        try:
            schema = pydantic_model.model_json_schema()
            schema_instruction = (
                "You MUST respond with ONLY a valid JSON object matching this schema. "
                "No markdown, no explanation, no text before or after the JSON.\n"
                f"Schema: {json.dumps(schema)}"
            )
            augmented_messages = list(messages)
            if augmented_messages and augmented_messages[0].get('role') == 'system':
                augmented_messages[0] = {
                    'role': 'system',
                    'content': augmented_messages[0]['content'] + "\n\n" + schema_instruction
                }
            else:
                augmented_messages.insert(0, {'role': 'system', 'content': schema_instruction})

            text_response, usage = await self.generate_text(
                augmented_messages,
                temperature,
                max_tokens,
                reasoning_effort=resolved_reasoning_effort,
                return_usage=True,
            )
            billed_usages.append(usage)
            data = self._extract_json_from_text(text_response)
            result = pydantic_model(**data)
            logger.debug(f"Tier 3 (text + JSON extraction) success: {type(result)}")
            if return_usage:
                return result, _combine_token_usage(billed_usages)
            return result

        except Exception as e:
            logger.error(f"All 3 tiers failed for model {self.model}. Tier 3 error: {e}")
            raise

    async def _generate_json_mode(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Tuple[BaseModel, Optional[TokenUsage]]:
        """Tier 2: Use JSON mode with schema instructions injected into the prompt.

        Most OpenAI-compatible APIs support response_format={"type": "json_object"},
        which constrains the model to output valid JSON (but without schema enforcement).
        We inject the JSON schema into the prompt so the model knows what to produce.
        """
        augmented_messages = self._augment_messages_for_json_mode(messages, pydantic_model)

        kwargs = {
            "model": self.model,
            "messages": augmented_messages,
            "response_format": {"type": "json_object"},
            "temperature": temperature
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        kwargs.update(self._max_tokens_kwargs(max_tokens))

        response = await self.client.chat.completions.create(**kwargs)

        usage = None
        if hasattr(response, 'usage') and response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                model=self.model
            )

        text = response.choices[0].message.content
        try:
            if not text:
                raise ValueError("JSON mode returned empty response")

            data = json.loads(text)
            result = pydantic_model(**data)
        except Exception as exc:
            raise StructuredGenerationError(str(exc), usage=usage) from exc
        return result, usage

    @staticmethod
    def _augment_messages_for_json_mode(
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
    ) -> List[Dict[str, str]]:
        """Inject JSON schema instructions into the message list."""
        schema = pydantic_model.model_json_schema()
        schema_instruction = (
            "You MUST respond with valid JSON matching this schema exactly. "
            "Do NOT include any text outside the JSON object.\n"
            f"JSON Schema:\n```json\n{json.dumps(schema, indent=2)}\n```"
        )

        augmented_messages = list(messages)
        if augmented_messages and augmented_messages[0].get('role') == 'system':
            augmented_messages[0] = {
                'role': 'system',
                'content': augmented_messages[0]['content'] + "\n\n" + schema_instruction
            }
        else:
            augmented_messages.insert(0, {'role': 'system', 'content': schema_instruction})
        return augmented_messages

    def supports_batch(self) -> bool:
        """Return whether this provider can use OpenAI's native batch API."""
        return not self._is_third_party

    def build_batch_chat_request(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a raw /v1/chat/completions request body for a batch line."""
        request_body: Dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 1.0 if self.model.startswith("gpt-5") else temperature,
        }
        resolved_reasoning_effort = self._resolve_reasoning_effort(reasoning_effort)
        if resolved_reasoning_effort is not None:
            request_body["reasoning_effort"] = resolved_reasoning_effort
        if pydantic_model is not None:
            request_body["response_format"] = self._json_schema_response_format(
                pydantic_model
            )
        request_body.update(self._max_tokens_kwargs(max_tokens))
        return request_body

    @staticmethod
    def _json_schema_response_format(pydantic_model: Type[BaseModel]) -> Dict[str, Any]:
        """Build an OpenAI structured-output response_format for batch requests."""
        schema = json.loads(json.dumps(pydantic_model.model_json_schema()))
        OpenAIProvider._add_additional_properties_false(schema)
        schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", pydantic_model.__name__)[:64]
        if not schema_name:
            schema_name = "DoctrailStructuredOutput"
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        }

    @staticmethod
    def _add_additional_properties_false(schema: Dict[str, Any]) -> None:
        """Recursively close object schemas for OpenAI strict structured output."""
        if not isinstance(schema, dict):
            return

        if schema.get("type") == "object":
            schema["additionalProperties"] = False

        for key in ("properties", "items", "$defs", "definitions"):
            val = schema.get(key)
            if isinstance(val, dict):
                for sub_schema in val.values():
                    OpenAIProvider._add_additional_properties_false(sub_schema)
            elif isinstance(val, list):
                for sub_schema in val:
                    OpenAIProvider._add_additional_properties_false(sub_schema)

        for key in ("anyOf", "oneOf", "allOf"):
            val = schema.get(key)
            if isinstance(val, list):
                for sub_schema in val:
                    OpenAIProvider._add_additional_properties_false(sub_schema)

    @staticmethod
    def _extract_json_from_text(text: str) -> dict:
        """Extract a JSON object from text that may contain markdown, preamble, etc.

        Handles common patterns from LLMs that wrap JSON in explanatory text:
        - Markdown code blocks: ```json ... ``` or ``` ... ```
        - JSON preceded/followed by explanatory text
        - Plain JSON string
        """
        if not text or not text.strip():
            raise ValueError("Empty text, cannot extract JSON")

        # Strategy 1: Try the raw text as-is
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # Strategy 2: Extract from markdown code blocks (```json ... ``` or ``` ... ```)
        code_block_pattern = r'```(?:json)?\s*\n?(.*?)\n?\s*```'
        matches = re.findall(code_block_pattern, text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

        # Strategy 3: Find the first { ... } block (greedy from first { to last })
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace:last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        raise ValueError(
            f"Could not extract JSON from response: {text[:300]}"
        )

    @staticmethod
    def extract_chat_completion_text(response_body: Dict[str, Any]) -> str:
        """Extract assistant text content from a chat completions payload."""
        try:
            message = response_body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected chat completion payload: {response_body}") from exc

        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            if text_parts:
                return "".join(text_parts)
        raise ValueError(f"Chat completion response did not contain text content: {response_body}")

    @staticmethod
    def _usage_attr(value: Any, attr: str, default: Any = None) -> Any:
        """Read an attribute from either a dict or an SDK object."""
        if isinstance(value, dict):
            return value.get(attr, default)
        return getattr(value, attr, default)

    @staticmethod
    def _cached_input_tokens_from_usage(usage_payload: Dict[str, Any]) -> int:
        """Extract cached input tokens from either chat or aggregate batch usage."""
        if not usage_payload:
            return 0

        prompt_details = OpenAIProvider._usage_attr(usage_payload, "prompt_tokens_details") or {}
        input_details = OpenAIProvider._usage_attr(usage_payload, "input_tokens_details") or {}
        return (
            OpenAIProvider._usage_attr(prompt_details, "cached_tokens")
            or OpenAIProvider._usage_attr(input_details, "cached_tokens")
            or OpenAIProvider._usage_attr(usage_payload, "cached_input_tokens")
            or OpenAIProvider._usage_attr(usage_payload, "input_cached_tokens")
            or 0
        )

    def build_token_usage(
        self,
        usage_payload: Optional[Dict[str, Any]],
        *,
        batch_pricing: bool = False,
    ) -> Optional[TokenUsage]:
        """Normalize token usage payloads from OpenAI chat and batch APIs."""
        if not usage_payload:
            return None

        input_tokens = self._usage_attr(usage_payload, "prompt_tokens")
        if input_tokens is None:
            input_tokens = self._usage_attr(usage_payload, "input_tokens", 0)

        output_tokens = self._usage_attr(usage_payload, "completion_tokens")
        if output_tokens is None:
            output_tokens = self._usage_attr(usage_payload, "output_tokens", 0)

        return TokenUsage(
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            model=self.model,
            cached_input_tokens=self._cached_input_tokens_from_usage(usage_payload),
            batch_pricing=batch_pricing,
        )

    @staticmethod
    def _validation_error_is_null_only(exc: ValidationError) -> bool:
        """Return True when every validation error was caused by a null input."""
        errors = exc.errors()
        return bool(errors) and all(error.get("input") is None for error in errors)

    def parse_batch_chat_response(
        self,
        response_body: Dict[str, Any],
        pydantic_model: Optional[Type[BaseModel]] = None,
        nullable_pydantic_model: Optional[Type[BaseModel]] = None,
    ) -> Tuple[Any, Optional[TokenUsage], str]:
        """Normalize one raw chat completion body returned from a batch output file."""
        text = self.extract_chat_completion_text(response_body)
        usage_payload = response_body.get("usage") or {}
        usage = self.build_token_usage(usage_payload, batch_pricing=True)

        if pydantic_model is None:
            return text, usage, json.dumps({"result": text}, ensure_ascii=False)

        data = self._extract_json_from_text(text)
        validation_warning = None
        try:
            parsed = pydantic_model(**data)
        except ValidationError as exc:
            if nullable_pydantic_model is None or not self._validation_error_is_null_only(exc):
                raise
            validation_warning = str(exc)
            parsed = nullable_pydantic_model(**data)
        raw_payload = dict(data)
        if validation_warning:
            raw_payload["validation_warning"] = validation_warning
        return parsed.model_dump(mode="json"), usage, json.dumps(raw_payload, ensure_ascii=False)
    
    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        return_usage: bool = False,
    ) -> Union[str, Tuple[str, Optional[TokenUsage]]]:
        """Generate unstructured text output."""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        kwargs.update(self._max_tokens_kwargs(max_tokens))
        resolved_reasoning_effort = self._resolve_reasoning_effort(reasoning_effort)
        if resolved_reasoning_effort is not None:
            kwargs["reasoning_effort"] = resolved_reasoning_effort
        response = await self.client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content
        if return_usage:
            return text, self.build_token_usage(getattr(response, "usage", None))
        return text
    
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
