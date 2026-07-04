"""Gemini provider implementation with structured output and batch support."""

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Dict, Any, Type, Optional, List, Tuple, Union
from dataclasses import dataclass
from pydantic import BaseModel, ValidationError
from google import genai
from ..utils.logging_config import suppress_noisy_loggers
from ..utils.model_pricing import canonicalize_model_name

logger = logging.getLogger(__name__)
suppress_noisy_loggers()


@dataclass
class TokenUsage:
    """Token usage information from LLM API calls."""
    input_tokens: int
    output_tokens: int
    model: str
    thought_tokens: int = 0
    cached_input_tokens: int = 0
    batch_pricing: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimate_cost(self) -> float:
        """Estimate cost in USD based on model pricing."""
        from ..utils.model_pricing import get_model_price
        input_price, output_price = get_model_price(self.model)
        if self.batch_pricing:
            input_price *= 0.5
            output_price *= 0.5
        if input_price == 0.0 and output_price == 0.0:
            logger.warning(f"Unknown model {self.model} for pricing, cost will show as $0")
        input_cost = (self.input_tokens / 1_000_000) * input_price
        output_cost = (self.output_tokens / 1_000_000) * output_price
        return input_cost + output_cost

class GeminiProvider:
    """Google Gemini LLM provider."""
    
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.client = genai.Client(api_key=api_key)
        self.model = canonicalize_model_name(model).removeprefix("models/")
        self.batch_api_base_url = "https://generativelanguage.googleapis.com/v1beta"
        
        # Model context limits
        self.context_limits = {
            "gemini-1.5-flash": 1048576,  # 1M tokens
            "gemini-1.5-flash-8b": 1048576,  # 1M tokens
            "gemini-1.5-pro": 2097152,  # 2M tokens
            "gemini-2.0-flash": 1048576,  # 1M tokens
            "gemini-2.5-flash": 1048576,  # 1M tokens
            "gemini-2.5-flash-lite": 1048576,  # 1M tokens
            "gemini-2.5-pro": 1048576,  # 1M tokens
            "gemini-3.1-pro-preview": 1048576,  # 1M tokens
            "gemini-3.1-flash-lite-preview": 1048576,  # 1M tokens
            "gemini-3.5-flash": 1048576,  # 1M tokens
            "gemini-pro": 32768,  # 32K tokens
        }

    @staticmethod
    def _to_json_dict(value: Any) -> Dict[str, Any]:
        """Normalize SDK models and plain dicts into JSON-native dicts."""
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        to_json_dict = getattr(value, "to_json_dict", None)
        if callable(to_json_dict):
            return to_json_dict()
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        raise TypeError(f"Unsupported Gemini SDK payload type: {type(value)!r}")

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """Convert lowerCamelCase keys to snake_case for JSONL file requests."""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    @classmethod
    def _keys_to_snake_case(cls, value: Any) -> Any:
        """Recursively convert dict keys to snake_case."""
        if isinstance(value, dict):
            return {
                cls._camel_to_snake(str(key)): cls._keys_to_snake_case(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._keys_to_snake_case(item) for item in value]
        return value
    
    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        return_usage: bool = False
    ) -> Union[BaseModel, Tuple[BaseModel, Optional[TokenUsage]]]:
        """Generate structured output using Gemini's response_schema.

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

        # Convert messages to Gemini format
        content = self._format_messages(messages)

        logger.debug(f"Gemini structured output with model: {pydantic_model.__name__}")
        logger.debug(f"Schema fields: {list(pydantic_model.model_fields.keys())}")

        usage = None

        try:
            # Gemini API doesn't support additionalProperties in JSON schemas,
            # but Pydantic models with extra='forbid' emit it. Strip it out.
            schema = self._clean_schema_for_gemini(pydantic_model)

            # Generate with structured output
            import asyncio
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model,
                contents=content,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                    "temperature": temperature,
                    "max_output_tokens": max_tokens
                }
            )

            # Extract token usage from response if available
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                candidate_tokens = response.usage_metadata.candidates_token_count or 0
                thought_tokens = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0
                usage = TokenUsage(
                    input_tokens=response.usage_metadata.prompt_token_count or 0,
                    output_tokens=candidate_tokens + thought_tokens,
                    model=self.model,
                    thought_tokens=thought_tokens,
                )
                logger.debug(f"Token usage: {usage.input_tokens} in, {usage.output_tokens} out, est. ${usage.estimate_cost():.4f}")

            # Always parse via JSON text to get a Pydantic model instance.
            # (When passing a dict schema, .parsed returns a dict, not a
            # Pydantic model, which breaks downstream .model_dump() calls.)
            import json

            text = self.extract_generate_content_text(response)
            if text:
                data = self._extract_json_from_text(text)
                result = pydantic_model(**data)
                logger.debug(f"Gemini structured output success via JSON parsing")
                if return_usage:
                    return result, usage
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
        
        return self.extract_generate_content_text(response)
    
    @staticmethod
    def _strip_additional_properties(obj):
        """Recursively remove 'additionalProperties' from a JSON schema dict."""
        if isinstance(obj, dict):
            obj.pop("additionalProperties", None)
            for v in obj.values():
                GeminiProvider._strip_additional_properties(v)
        elif isinstance(obj, list):
            for item in obj:
                GeminiProvider._strip_additional_properties(item)

    @staticmethod
    def _inline_schema_refs(obj: Any, defs: Dict[str, Any]) -> Any:
        """Inline local $defs references for Gemini's OpenAPI-style Schema."""
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref = obj["$ref"]
                prefix = "#/$defs/"
                if ref.startswith(prefix):
                    name = ref[len(prefix):]
                    if name not in defs:
                        raise ValueError(f"Gemini schema referenced missing definition: {ref}")
                    resolved = GeminiProvider._inline_schema_refs(
                        json.loads(json.dumps(defs[name])),
                        defs,
                    )
                    siblings = {
                        key: GeminiProvider._inline_schema_refs(value, defs)
                        for key, value in obj.items()
                        if key != "$ref"
                    }
                    if isinstance(resolved, dict):
                        resolved.update(siblings)
                        return resolved
                    return resolved
                raise ValueError(f"Gemini schema contains unsupported reference: {ref}")

            return {
                key: GeminiProvider._inline_schema_refs(value, defs)
                for key, value in obj.items()
                if key not in {"$defs", "definitions"}
            }

        if isinstance(obj, list):
            return [GeminiProvider._inline_schema_refs(item, defs) for item in obj]

        return obj

    def _clean_schema_for_gemini(self, pydantic_model: Type[BaseModel]) -> dict:
        """Convert Pydantic model to a Gemini-compatible JSON schema dict.

        Gemini's ``responseSchema`` field accepts an OpenAPI-style schema object,
        not full JSON Schema. Strip ``additionalProperties`` and inline local
        Pydantic references so enums and nested objects are submitted directly.
        """
        schema = json.loads(json.dumps(pydantic_model.model_json_schema()))
        defs = schema.get("$defs", {})
        schema = self._inline_schema_refs(schema, defs)
        self._strip_additional_properties(schema)
        logger.debug(f"Cleaned Gemini schema: {list(schema.get('properties', {}).keys())}")
        return schema

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

    @staticmethod
    def _usage_attr(value: Any, attr: str, default: Any = None) -> Any:
        """Read an attribute from either a dict or an SDK object."""
        if isinstance(value, dict):
            return value.get(attr, default)
        return getattr(value, attr, default)

    @staticmethod
    def _parse_int(value: Any) -> int:
        """Safely parse provider counters that may be strings."""
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalize_batch_state(state: Optional[str]) -> Optional[str]:
        """Normalize legacy JOB_STATE values to the live BATCH_STATE namespace."""
        if not state:
            return state
        if state.startswith("JOB_STATE_"):
            return state.replace("JOB_STATE_", "BATCH_STATE_", 1)
        return state

    def supports_batch(self) -> bool:
        """Return whether this provider can use Gemini's native batch API."""
        return True

    def build_batch_generate_content_request(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build one raw GenerateContentRequest payload for Gemini batch submission."""
        request_body: Dict[str, Any] = {
            "contents": [{
                "role": "user",
                "parts": [{"text": self._format_messages(list(messages))}],
            }],
        }

        generation_config: Dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if pydantic_model is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = self._clean_schema_for_gemini(pydantic_model)
        request_body["generationConfig"] = generation_config
        return request_body

    def build_batch_generate_content_file_request(
        self,
        *,
        key: str,
        messages: List[Dict[str, str]],
        pydantic_model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build one JSONL line for Gemini's file-backed batch API."""
        request_body = self.build_batch_generate_content_request(
            messages=messages,
            pydantic_model=pydantic_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return {
            "key": key,
            "request": self._keys_to_snake_case(request_body),
        }

    async def _batch_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Issue a raw Gemini batch REST request using the API key auth flow."""

        def _do_request() -> Dict[str, Any]:
            request_url = f"{self.batch_api_base_url}/{path.lstrip('/')}"
            separator = "&" if "?" in request_url else "?"
            request_url = f"{request_url}{separator}key={self.api_key}"
            headers = {}
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                request_url,
                data=data,
                headers=headers,
                method=method.upper(),
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Gemini batch request failed ({method.upper()} {path}): "
                    f"{exc.code} {error_body}"
                ) from exc
            return json.loads(raw) if raw else {}

        return await asyncio.to_thread(_do_request)

    def _normalize_batch_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Gemini REST batch objects into a provider-stable shape."""
        payload = self._to_json_dict(payload)
        metadata = payload.get("metadata") or {}
        response = payload.get("response") or {}
        dest = payload.get("dest") or {}
        inline_responses = (
            response.get("inlinedResponses")
            or response.get("inlined_responses")
            or dest.get("inlinedResponses")
            or dest.get("inlined_responses")
            or []
        )
        batch_stats = (
            metadata.get("batchStats")
            or payload.get("batchStats")
            or payload.get("batch_stats")
            or {}
        )

        normalized_stats = {
            "request_count": self._parse_int(batch_stats.get("requestCount")),
            "pending_request_count": self._parse_int(batch_stats.get("pendingRequestCount")),
            "running_request_count": self._parse_int(
                batch_stats.get("runningRequestCount") or batch_stats.get("processingRequestCount")
            ),
            "succeeded_request_count": self._parse_int(
                batch_stats.get("succeededRequestCount") or batch_stats.get("successfulRequestCount")
            ),
            "failed_request_count": self._parse_int(batch_stats.get("failedRequestCount")),
            "cancelled_request_count": self._parse_int(
                batch_stats.get("cancelledRequestCount") or batch_stats.get("canceledRequestCount")
            ),
            "expired_request_count": self._parse_int(batch_stats.get("expiredRequestCount")),
        }
        raw_state = metadata.get("state") or payload.get("state")
        if isinstance(raw_state, dict):
            raw_state = raw_state.get("name")
        state = self._normalize_batch_state(raw_state)

        if normalized_stats["request_count"] == 0 and inline_responses:
            normalized_stats["request_count"] = len(inline_responses)

        if inline_responses and normalized_stats["succeeded_request_count"] == 0:
            normalized_stats["succeeded_request_count"] = sum(
                1 for item in inline_responses if isinstance(item, dict) and item.get("response")
            )
        if inline_responses and normalized_stats["failed_request_count"] == 0:
            normalized_stats["failed_request_count"] = sum(
                1 for item in inline_responses if isinstance(item, dict) and item.get("error")
            )

        total = normalized_stats["request_count"]
        accounted = (
            normalized_stats["succeeded_request_count"]
            + normalized_stats["failed_request_count"]
            + normalized_stats["cancelled_request_count"]
            + normalized_stats["expired_request_count"]
        )
        remaining = max(total - accounted, 0)
        if state == "BATCH_STATE_CANCELLED" and remaining:
            normalized_stats["cancelled_request_count"] += remaining
            normalized_stats["pending_request_count"] = 0
            normalized_stats["running_request_count"] = 0
        elif state == "BATCH_STATE_EXPIRED" and remaining:
            normalized_stats["expired_request_count"] += remaining
            normalized_stats["pending_request_count"] = 0
            normalized_stats["running_request_count"] = 0
        elif state == "BATCH_STATE_FAILED" and remaining:
            normalized_stats["failed_request_count"] += remaining
            normalized_stats["pending_request_count"] = 0
            normalized_stats["running_request_count"] = 0

        return {
            "id": payload.get("name"),
            "name": payload.get("name"),
            "state": state,
            "completed_at": (
                metadata.get("endTime")
                or payload.get("endTime")
                or payload.get("end_time")
                or payload.get("completedAt")
                or payload.get("completed_at")
            ),
            "updated_at": (
                metadata.get("updateTime")
                or payload.get("updateTime")
                or payload.get("update_time")
            ),
            "error": payload.get("error") or metadata.get("error"),
            "batch_stats": normalized_stats,
            "dest": {
                "file_name": (
                    response.get("responsesFile")
                    or response.get("fileName")
                    or dest.get("fileName")
                    or dest.get("file_name")
                ),
                "inlined_responses": inline_responses,
            },
        }

    async def create_batch_job(
        self,
        src: str,
        *,
        display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Gemini batch job using an uploaded JSONL file."""
        # The current google-genai SDK exposes files.upload/download for the
        # Gemini File API, but its batches.create helper still only supports
        # GCS and BigQuery sources. Use raw REST for file-backed batch submit.
        batch_payload: Dict[str, Any] = {
            "batch": {
                "input_config": {
                    "file_name": src,
                }
            }
        }
        if display_name:
            batch_payload["batch"]["display_name"] = display_name

        payload = await self._batch_request(
            "POST",
            f"models/{self.model}:batchGenerateContent",
            batch_payload,
        )
        return self._normalize_batch_job(payload)

    async def get_batch_job(self, name: str) -> Dict[str, Any]:
        """Fetch the latest Gemini batch job status and results."""
        payload = await self._batch_request("GET", name)
        return self._normalize_batch_job(payload)

    async def cancel_batch_job(self, name: str) -> Dict[str, Any]:
        """Cancel a Gemini batch job and return the refreshed job state."""
        await self._batch_request("POST", f"{name}:cancel", {})
        return await self.get_batch_job(name)

    async def upload_batch_requests_file(
        self,
        file_path: str,
        *,
        display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a JSONL batch input file through Gemini's File API."""
        config: Dict[str, Any] = {"mime_type": "jsonl"}
        if display_name:
            config["display_name"] = display_name
        uploaded = await asyncio.to_thread(
            self.client.files.upload,
            file=file_path,
            config=config,
        )
        return self._to_json_dict(uploaded)

    async def download_batch_result_file(self, file_name: str) -> str:
        """Download a Gemini batch result JSONL file as UTF-8 text."""
        content = await asyncio.to_thread(self.client.files.download, file=file_name)
        if isinstance(content, bytes):
            return content.decode("utf-8")
        if isinstance(content, str):
            return content
        raise TypeError(f"Unexpected Gemini file download payload: {type(content)!r}")

    def build_batch_request_counts(self, batch_stats: Optional[Dict[str, Any]]) -> Dict[str, int]:
        """Translate Gemini batch stats into Doctrail's normalized request counts."""
        batch_stats = batch_stats or {}
        return {
            "total": self._parse_int(batch_stats.get("request_count")),
            "processing": (
                self._parse_int(batch_stats.get("pending_request_count"))
                + self._parse_int(batch_stats.get("running_request_count"))
            ),
            "succeeded": self._parse_int(batch_stats.get("succeeded_request_count")),
            "errored": self._parse_int(batch_stats.get("failed_request_count")),
            "canceled": self._parse_int(batch_stats.get("cancelled_request_count")),
            "expired": self._parse_int(batch_stats.get("expired_request_count")),
        }

    def build_token_usage(
        self,
        usage_payload: Optional[Dict[str, Any]],
        *,
        batch_pricing: bool = False,
    ) -> Optional[TokenUsage]:
        """Normalize Gemini usage metadata from raw responses."""
        if not usage_payload:
            return None

        input_tokens = self._parse_int(
            self._usage_attr(usage_payload, "promptTokenCount")
            or self._usage_attr(usage_payload, "prompt_token_count")
        )
        output_tokens = self._parse_int(
            self._usage_attr(usage_payload, "candidatesTokenCount")
            or self._usage_attr(usage_payload, "candidates_token_count")
        )
        thought_tokens = self._parse_int(
            self._usage_attr(usage_payload, "thoughtsTokenCount")
            or self._usage_attr(usage_payload, "thoughts_token_count")
            or self._usage_attr(usage_payload, "thoughtTokenCount")
            or self._usage_attr(usage_payload, "thought_token_count")
        )
        cached_tokens = self._parse_int(
            self._usage_attr(usage_payload, "cachedContentTokenCount")
            or self._usage_attr(usage_payload, "cached_content_token_count")
        )

        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens + thought_tokens,
            thought_tokens=thought_tokens,
            cached_input_tokens=cached_tokens,
            model=self.model,
            batch_pricing=batch_pricing,
        )

    @classmethod
    def extract_generate_content_text(cls, response_payload: Dict[str, Any]) -> str:
        """Extract text from a raw Gemini GenerateContentResponse payload."""
        if not response_payload:
            raise ValueError("Gemini batch response was empty")

        if isinstance(response_payload, dict):
            direct_text = cls._usage_attr(response_payload, "text")
            if isinstance(direct_text, str) and direct_text:
                return direct_text

        candidates = cls._usage_attr(response_payload, "candidates") or []
        text_parts: List[str] = []
        for candidate in candidates:
            content = cls._usage_attr(candidate, "content") or {}
            for part in cls._usage_attr(content, "parts") or []:
                if isinstance(part, dict):
                    text = part.get("text")
                else:
                    try:
                        text = cls._to_json_dict(part).get("text")
                    except TypeError:
                        text = cls._usage_attr(part, "text")
                if text:
                    text_parts.append(text)

        if text_parts:
            return "".join(text_parts)

        raise ValueError(f"Gemini batch response did not contain text content: {response_payload}")

    @staticmethod
    def _validation_error_is_null_only(exc: ValidationError) -> bool:
        """Return True when every validation error was caused by a null input."""
        errors = exc.errors()
        return bool(errors) and all(error.get("input") is None for error in errors)

    @staticmethod
    def _extract_json_from_text(text: str) -> dict:
        """Extract a JSON object from text that may contain markdown or preamble."""
        if not text or not text.strip():
            raise ValueError("Empty text, cannot extract JSON")

        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        code_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
        matches = re.findall(code_block_pattern, text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace:last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from response: {text[:300]}")

    def parse_batch_generate_content_response(
        self,
        response_payload: Dict[str, Any],
        pydantic_model: Optional[Type[BaseModel]] = None,
        nullable_pydantic_model: Optional[Type[BaseModel]] = None,
    ) -> Tuple[Any, Optional[TokenUsage], str]:
        """Normalize one Gemini batch inline response into Doctrail's storage contract."""
        text = self.extract_generate_content_text(response_payload)
        usage_payload = (
            self._usage_attr(response_payload, "usageMetadata")
            or self._usage_attr(response_payload, "usage_metadata")
            or {}
        )
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
    
    def count_tokens(self, text: str) -> int:
        """Count tokens for Gemini models."""
        # Gemini uses roughly 1 token per 4 characters as an approximation
        # This is not exact but provides a reasonable estimate
        return len(text) // 4
    
    @property
    def max_context_tokens(self) -> int:
        """Maximum context window size."""
        return self.context_limits.get(self.model, 1048576)  # Default to 1M
