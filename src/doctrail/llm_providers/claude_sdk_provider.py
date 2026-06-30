"""Claude Agent SDK provider — uses claude-agent-sdk for clean structured output.

Routes cli/claude/* models through the Claude Agent SDK instead of raw subprocess
management. The SDK handles the Claude Code binary, JSON-RPC transport, and
structured output tool calls internally.

Auth:
    - If ANTHROPIC_API_KEY is set: uses API billing
    - If ANTHROPIC_API_KEY is unset: falls back to Claude subscription (OAuth)

The old subprocess-based CLIProvider is preserved in cli_provider.py but dormant
for Claude models.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Dict, Any, Type, Optional, List, Tuple, Union

from pydantic import BaseModel
from ..utils.model_pricing import canonicalize_model_name

logger = logging.getLogger(__name__)

# Lazy import to avoid hard dependency at module level
_sdk_imported = False
_query = None
_ClaudeAgentOptions = None
_ResultMessage = None
_AssistantMessage = None
_TextBlock = None
_ToolUseBlock = None


def _ensure_sdk():
    """Lazy-import claude_agent_sdk on first use."""
    global _sdk_imported, _query, _ClaudeAgentOptions, _ResultMessage
    global _AssistantMessage, _TextBlock, _ToolUseBlock
    if _sdk_imported:
        return
    try:
        from claude_agent_sdk import (
            query,
            ClaudeAgentOptions,
            ResultMessage,
            AssistantMessage,
            TextBlock,
            ToolUseBlock,
        )
        _query = query
        _ClaudeAgentOptions = ClaudeAgentOptions
        _ResultMessage = ResultMessage
        _AssistantMessage = AssistantMessage
        _TextBlock = TextBlock
        _ToolUseBlock = ToolUseBlock
        _sdk_imported = True
    except ImportError:
        raise ImportError(
            "claude-agent-sdk is required for cli/claude models. "
            "Install with: uv add claude-agent-sdk"
        )


# Model aliases (same as old CLI provider)
CLAUDE_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
    "claude-sonnet-4": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-haiku-4-5": "claude-haiku-4-5",
}

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_TURNS = 3


@dataclass
class TokenUsage:
    """Token usage from Claude SDK."""
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimate_cost(self) -> float:
        """SDK cost is tracked by the SDK itself — return 0 for CLI/subscription mode."""
        return 0.0


class ClaudeSDKProvider:
    """LLM provider using claude-agent-sdk for Claude models.

    Usage:
        provider = ClaudeSDKProvider(model="sonnet")
        result = await provider.generate_structured(messages, MyModel)
    """

    def __init__(self, model: str = None, max_turns: int = DEFAULT_TIMEOUT_TURNS):
        _ensure_sdk()
        raw_model = model or "sonnet"
        normalized_model = canonicalize_model_name(f"cli/claude/{raw_model}").removeprefix("cli/claude/")
        self.model = CLAUDE_ALIASES.get(normalized_model, normalized_model)
        self.max_turns = max_turns
        self._display_model = f"cli/claude/{normalized_model}"

    def _build_prompt(self, messages: List[Dict[str, str]]) -> Tuple[Optional[str], str]:
        """Separate system prompt from user content."""
        system_parts = []
        user_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append(content)
            elif role == "assistant":
                user_parts.append(f"Assistant: {content}")
            else:
                user_parts.append(content)
        system = "\n\n".join(system_parts) if system_parts else None
        user = "\n\n".join(user_parts)
        return system, user

    async def _query_sdk(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        output_format: Optional[dict] = None,
    ) -> Tuple[Optional[str], Optional[dict], Optional[dict]]:
        """Run a query through the Claude Agent SDK.

        Returns (result_text, structured_output, usage_dict).
        """
        options = _ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt or "You are a helpful assistant.",
            max_turns=self.max_turns,
            tools=[],              # Strip all tool definitions from system prompt (~34K tokens saved)
            permission_mode="bypassPermissions",
            setting_sources=[],    # Skip loading CLAUDE.md, project settings
            mcp_servers={},        # No MCP servers
        )

        if output_format:
            options.output_format = output_format

        # Unset CLAUDECODE to avoid "nested session" error.
        # Also unset ANTHROPIC_API_KEY so the SDK uses OAuth (subscription)
        # instead of burning API credits.
        options.env = {"CLAUDECODE": "", "ANTHROPIC_API_KEY": ""}

        result_text = None
        structured_output = None
        usage_dict = None

        # Also collect structured output from ToolUseBlock as fallback
        tool_use_output = None

        from claude_agent_sdk import SystemMessage as _SystemMessage

        async for message in _query(prompt=prompt, options=options):
            # Safety check: verify we're on OAuth, not burning API credits
            if isinstance(message, _SystemMessage) and getattr(message, 'subtype', '') == 'init':
                data = message.data if isinstance(message.data, dict) else {}
                api_source = data.get('apiKeySource', '')
                if api_source and api_source != 'none':
                    logger.warning(
                        f"Claude SDK using API key ({api_source}) instead of OAuth. "
                        f"Unset ANTHROPIC_API_KEY to use subscription billing."
                    )
                else:
                    logger.debug("Claude SDK auth: OAuth (subscription)")
                continue

            if isinstance(message, _AssistantMessage):
                for block in message.content:
                    if isinstance(block, _ToolUseBlock) and block.name == "StructuredOutput":
                        tool_use_output = block.input
                    elif isinstance(block, _TextBlock):
                        # Capture text in case there's no structured output
                        if result_text is None:
                            result_text = block.text
                        else:
                            result_text += "\n" + block.text

            elif isinstance(message, _ResultMessage):
                if message.result:
                    result_text = message.result
                if message.structured_output:
                    structured_output = message.structured_output
                if hasattr(message, 'usage') and message.usage:
                    u = message.usage
                    usage_dict = {
                        'input_tokens': u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0),
                        'output_tokens': u.get('output_tokens', 0),
                    }

        # Fall back to ToolUseBlock output if structured_output wasn't set
        if structured_output is None and tool_use_output is not None:
            structured_output = tool_use_output

        return result_text, structured_output, usage_dict

    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        return_usage: bool = False,
    ) -> Union[BaseModel, Tuple[BaseModel, Optional[TokenUsage]]]:
        """Generate structured output via Claude Agent SDK."""
        logger.debug(f"SDK structured output: model={self.model}, schema={pydantic_model.__name__}")

        system_prompt, prompt = self._build_prompt(messages)
        schema = pydantic_model.model_json_schema()

        result_text, structured_output, usage_dict = await self._query_sdk(
            prompt=prompt,
            system_prompt=system_prompt,
            output_format={"type": "json_schema", "schema": schema},
        )

        if structured_output is not None:
            result = pydantic_model(**structured_output)
        else:
            raise ValueError(
                f"Claude SDK returned no structured output. "
                f"Result text: {(result_text or '')[:300]}"
            )

        usage = TokenUsage(
            input_tokens=usage_dict.get('input_tokens', 0) if usage_dict else len(prompt) // 4,
            output_tokens=usage_dict.get('output_tokens', 0) if usage_dict else 0,
            model=self._display_model,
        )

        logger.debug(f"SDK structured output success: {type(result).__name__}")
        if return_usage:
            return result, usage
        return result

    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate unstructured text via Claude Agent SDK."""
        system_prompt, prompt = self._build_prompt(messages)

        result_text, _, _ = await self._query_sdk(
            prompt=prompt,
            system_prompt=system_prompt,
        )

        return result_text or ""

    def count_tokens(self, text: str) -> int:
        """Approximate token count (~4 chars per token)."""
        return len(text) // 4

    @property
    def max_context_tokens(self) -> int:
        """Claude context window."""
        return 200000
