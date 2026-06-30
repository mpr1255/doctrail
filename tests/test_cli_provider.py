"""Unit tests for CLI-based LLM provider (claude, gemini, codex backends).

Tests structured output, text generation, error handling, and environment
variable management. All subprocess calls are mocked.
"""

import asyncio
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pydantic import BaseModel
from typing import List, Optional

from doctrail.llm_providers.cli_provider import (
    CLIProvider,
    TokenUsage,
    _extract_json_from_text,
    _add_additional_properties_false,
    _run_subprocess,
    CLAUDE_ALIASES,
    CLI_DEFAULTS,
)


# --- Test models ---

class SimpleResult(BaseModel):
    category: str
    confidence: float


class MultiFieldResult(BaseModel):
    hostility_level: int
    explanation: str
    tags: List[str]


# --- JSON extraction tests ---

class TestExtractJsonFromText:
    def test_plain_json(self):
        text = '{"category": "politics", "confidence": 0.9}'
        result = _extract_json_from_text(text)
        assert result == {"category": "politics", "confidence": 0.9}

    def test_markdown_code_block(self):
        text = '```json\n{"category": "science", "confidence": 0.8}\n```'
        result = _extract_json_from_text(text)
        assert result["category"] == "science"

    def test_json_with_preamble(self):
        text = 'Here is the result:\n\n{"category": "arts", "confidence": 0.7}\n\nDone.'
        result = _extract_json_from_text(text)
        assert result["category"] == "arts"

    def test_empty_text_raises(self):
        with pytest.raises(ValueError, match="Empty text"):
            _extract_json_from_text("")

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="Could not extract JSON"):
            _extract_json_from_text("This is just plain text with no JSON at all.")


# --- Schema helper tests ---

class TestAdditionalPropertiesFalse:
    def test_simple_object(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        _add_additional_properties_false(schema)
        assert schema["additionalProperties"] is False

    def test_nested_objects(self):
        schema = {
            "type": "object",
            "properties": {
                "inner": {"type": "object", "properties": {"val": {"type": "integer"}}}
            },
        }
        _add_additional_properties_false(schema)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["inner"]["additionalProperties"] is False

    def test_defs(self):
        schema = {
            "type": "object", "properties": {},
            "$defs": {"Sub": {"type": "object", "properties": {"x": {"type": "string"}}}},
        }
        _add_additional_properties_false(schema)
        assert schema["$defs"]["Sub"]["additionalProperties"] is False

    def test_non_object_unchanged(self):
        schema = {"type": "string"}
        _add_additional_properties_false(schema)
        assert "additionalProperties" not in schema


# --- CLIProvider init tests ---

class TestCLIProviderInit:
    def test_valid_tools(self):
        for tool in ("claude", "gemini", "codex"):
            p = CLIProvider(cli_tool=tool, model="test")
            assert p.cli_tool == tool

    def test_invalid_tool_raises(self):
        with pytest.raises(ValueError, match="Unknown CLI tool"):
            CLIProvider(cli_tool="gpt", model="test")

    def test_default_model(self):
        p = CLIProvider(cli_tool="claude", model=None)
        assert p.model == "sonnet"

    def test_context_limits(self):
        p = CLIProvider(cli_tool="gemini", model="flash")
        assert p.max_context_tokens == 1000000

    def test_token_count_approx(self):
        p = CLIProvider(cli_tool="claude", model="sonnet")
        assert p.count_tokens("hello world") == len("hello world") // 4


# --- TokenUsage tests ---

class TestTokenUsage:
    def test_cost_is_zero(self):
        usage = TokenUsage(input_tokens=1000, output_tokens=500, model="cli/claude/sonnet")
        assert usage.estimate_cost() == 0.0

    def test_total_tokens(self):
        usage = TokenUsage(input_tokens=100, output_tokens=200, model="test")
        assert usage.total_tokens == 300


# --- Mock subprocess helper ---

def _make_mock_process(stdout: str, stderr: str = "", returncode: int = 0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.returncode = returncode
    proc.kill = MagicMock()  # kill() is sync on real asyncio.Process
    proc.wait = AsyncMock()
    return proc


# --- Claude CLI tests ---

class TestClaudeCLI:
    @pytest.mark.asyncio
    async def test_structured_output_parses_from_envelope(self):
        """Claude returns JSON envelope; result field has model text with JSON."""
        # The model returns raw JSON text (schema injected into system prompt)
        result_text = '{"category": "politics", "confidence": 0.95}'
        envelope = json.dumps({"type": "result", "result": result_text})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            result = await provider.generate_structured(
                messages=[{"role": "user", "content": "Classify this"}],
                pydantic_model=SimpleResult,
            )

            assert isinstance(result, SimpleResult)
            assert result.category == "politics"
            assert result.confidence == 0.95

            # Verify key flags
            cmd_args = mock_exec.call_args[0]
            assert "claude" in cmd_args
            assert "-p" in cmd_args
            assert "--system-prompt" in cmd_args
            assert "--max-turns" in cmd_args
            assert "--no-session-persistence" in cmd_args
            assert "--allowedTools" in cmd_args

    @pytest.mark.asyncio
    async def test_schema_injected_into_system_prompt(self):
        """Schema instruction goes into --system-prompt, not --json-schema."""
        result_text = '{"category": "test", "confidence": 0.5}'
        envelope = json.dumps({"type": "result", "result": result_text})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            await provider.generate_structured(
                messages=[
                    {"role": "system", "content": "You are a classifier."},
                    {"role": "user", "content": "test"},
                ],
                pydantic_model=SimpleResult,
            )

            cmd_args = list(mock_exec.call_args[0])
            # --json-schema should NOT be in the command
            assert "--json-schema" not in cmd_args
            # --system-prompt should contain the schema instruction
            sys_idx = cmd_args.index("--system-prompt") + 1
            sys_prompt = cmd_args[sys_idx]
            assert "JSON" in sys_prompt
            assert "Schema" in sys_prompt
            assert "classifier" in sys_prompt  # user's system prompt preserved

    @pytest.mark.asyncio
    async def test_claude_unsets_claudecode(self):
        """Must unset CLAUDECODE env var."""
        envelope = json.dumps({"type": "result", "result": '{"category": "x", "confidence": 0.1}'})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            await provider.generate_structured(
                messages=[{"role": "user", "content": "test"}],
                pydantic_model=SimpleResult,
            )

            call_kwargs = mock_exec.call_args[1]
            env = call_kwargs.get("env", {})
            assert "CLAUDECODE" not in env

    @pytest.mark.asyncio
    async def test_claude_model_alias(self):
        envelope = json.dumps({"type": "result", "result": '{"category": "x", "confidence": 0.1}'})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            provider = CLIProvider(cli_tool="claude", model="opus")
            await provider.generate_structured(
                messages=[{"role": "user", "content": "test"}],
                pydantic_model=SimpleResult,
            )
            assert "opus" in mock_exec.call_args[0]

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self):
        mock_proc = _make_mock_process(stdout="", stderr="Error: rate limited", returncode=1)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            with pytest.raises(RuntimeError, match="claude CLI exited 1"):
                await provider.generate_structured(
                    messages=[{"role": "user", "content": "test"}],
                    pydantic_model=SimpleResult,
                )

    @pytest.mark.asyncio
    async def test_generate_text(self):
        envelope = json.dumps({"type": "result", "result": "This is a summary."})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            result = await provider.generate_text(
                messages=[{"role": "user", "content": "Summarize this"}],
            )
            assert result == "This is a summary."

    @pytest.mark.asyncio
    async def test_envelope_result_null_returns_empty(self):
        """If result is null in envelope, return empty string (don't crash)."""
        envelope = json.dumps({"type": "result", "result": None})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            result = await provider.generate_text(
                messages=[{"role": "user", "content": "test"}],
            )
            assert result == ""


# --- Gemini CLI tests ---

class TestGeminiCLI:
    @pytest.mark.asyncio
    async def test_structured_output(self):
        raw_json = json.dumps({"category": "science", "confidence": 0.88})
        mock_proc = _make_mock_process(stdout=raw_json)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            provider = CLIProvider(cli_tool="gemini", model="gemini-2.5-flash")
            result = await provider.generate_structured(
                messages=[
                    {"role": "system", "content": "You are a classifier."},
                    {"role": "user", "content": "Classify this document."},
                ],
                pydantic_model=SimpleResult,
            )

            assert isinstance(result, SimpleResult)
            assert result.category == "science"

            cmd_args = mock_exec.call_args[0]
            assert "gemini" in cmd_args
            assert "-m" in cmd_args

    @pytest.mark.asyncio
    async def test_schema_in_prompt(self):
        """Gemini has no --json-schema, so schema goes into the prompt text."""
        raw_json = json.dumps({"category": "x", "confidence": 0.1})
        mock_proc = _make_mock_process(stdout=raw_json)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            provider = CLIProvider(cli_tool="gemini", model="gemini-2.5-flash")
            await provider.generate_structured(
                messages=[{"role": "user", "content": "test"}],
                pydantic_model=SimpleResult,
            )

            cmd_args = mock_exec.call_args[0]
            prompt_idx = list(cmd_args).index("-p") + 1
            prompt_text = cmd_args[prompt_idx]
            assert "JSON" in prompt_text
            assert "Schema" in prompt_text

    @pytest.mark.asyncio
    async def test_markdown_response(self):
        raw = '```json\n{"category": "tech", "confidence": 0.75}\n```'
        mock_proc = _make_mock_process(stdout=raw)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="gemini", model="gemini-2.5-flash")
            result = await provider.generate_structured(
                messages=[{"role": "user", "content": "test"}],
                pydantic_model=SimpleResult,
            )
            assert result.category == "tech"

    @pytest.mark.asyncio
    async def test_generate_text(self):
        mock_proc = _make_mock_process(stdout="Climate change discussion.")

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="gemini", model="gemini-2.5-flash")
            result = await provider.generate_text(
                messages=[{"role": "user", "content": "Summarize"}],
            )
            assert "Climate" in result


# --- Codex CLI tests ---

class TestCodexCLI:
    @pytest.mark.asyncio
    async def test_structured_output(self):
        raw_json = json.dumps({"category": "economics", "confidence": 0.92})
        mock_proc = _make_mock_process(stdout=raw_json)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec, \
             patch("os.path.exists", return_value=False):
            provider = CLIProvider(cli_tool="codex", model="o3-mini")
            result = await provider.generate_structured(
                messages=[{"role": "user", "content": "Classify"}],
                pydantic_model=SimpleResult,
            )

            assert isinstance(result, SimpleResult)
            assert result.category == "economics"
            cmd_args = mock_exec.call_args[0]
            assert "codex" in cmd_args
            assert "exec" in cmd_args
            assert "--sandbox" in cmd_args
            assert "--ignore-user-config" in cmd_args
            assert "--ignore-rules" in cmd_args
            assert "--disable" in cmd_args
            assert "-c" in cmd_args
            cmd_str = " ".join(str(a) for a in cmd_args)
            assert "shell_tool" in cmd_str
            assert "browser_use" in cmd_str
            assert "multi_agent" in cmd_str
            assert "model_reasoning_effort" in cmd_str
            assert "personality" in cmd_str

    @pytest.mark.asyncio
    async def test_generate_text(self):
        mock_proc = _make_mock_process(stdout="Summary of the text.")

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="codex", model="o3-mini")
            result = await provider.generate_text(
                messages=[{"role": "user", "content": "Summarize"}],
            )
            assert result == "Summary of the text."


# --- Factory routing tests ---

class TestFactoryRouting:
    def test_cli_claude(self):
        from doctrail.llm_providers.factory import get_llm_provider
        from doctrail.llm_providers.claude_sdk_provider import ClaudeSDKProvider
        p = get_llm_provider("cli/claude/sonnet")
        assert isinstance(p, ClaudeSDKProvider)
        assert p.model == "claude-sonnet-4-6"

    def test_cli_claude_dotted_alias(self):
        from doctrail.llm_providers.factory import get_llm_provider
        from doctrail.llm_providers.claude_sdk_provider import ClaudeSDKProvider
        p = get_llm_provider("cli/claude/claude-haiku-4.5")
        assert isinstance(p, ClaudeSDKProvider)
        assert p.model == "claude-haiku-4-5"

    def test_cli_gemini(self):
        from doctrail.llm_providers.factory import get_llm_provider
        p = get_llm_provider("cli/gemini/gemini-2.5-flash")
        assert isinstance(p, CLIProvider)
        assert p.cli_tool == "gemini"

    def test_cli_codex(self):
        from doctrail.llm_providers.factory import get_llm_provider
        p = get_llm_provider("cli/codex/o3-mini")
        assert isinstance(p, CLIProvider)
        assert p.cli_tool == "codex"

    def test_cli_default_model(self):
        from doctrail.llm_providers.factory import get_llm_provider
        from doctrail.llm_providers.claude_sdk_provider import ClaudeSDKProvider
        p = get_llm_provider("cli/claude")
        assert isinstance(p, ClaudeSDKProvider)
        assert p.model == "claude-sonnet-4-6"

    def test_non_cli_not_affected(self):
        from doctrail.llm_providers.factory import get_llm_provider
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            p = get_llm_provider("claude-sonnet-4")
            from doctrail.llm_providers.anthropic_provider import AnthropicProvider
            assert isinstance(p, AnthropicProvider)

    def test_direct_claude_dotted_alias(self):
        from doctrail.llm_providers.factory import get_llm_provider
        from doctrail.llm_providers.anthropic_provider import AnthropicProvider
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            p = get_llm_provider("claude-haiku-4.5")
            assert isinstance(p, AnthropicProvider)
            assert p.model == "claude-haiku-4-5"


# --- Subprocess helper tests ---

class TestRunSubprocess:
    @pytest.mark.asyncio
    async def test_env_var_removal(self):
        mock_proc = _make_mock_process(stdout="ok")

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc) as mock_exec:
            with patch.dict(os.environ, {"CLAUDECODE": "1"}):
                await _run_subprocess(["echo", "test"], env={"CLAUDECODE": None})

                passed_env = mock_exec.call_args[1]["env"]
                assert "CLAUDECODE" not in passed_env

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=proc):
            with pytest.raises(TimeoutError, match="timed out"):
                await _run_subprocess(["slow", "cmd"], timeout=1)
            proc.kill.assert_called_once()


# --- Message formatting tests ---

class TestMessageFormatting:
    def test_build_prompt_text(self):
        provider = CLIProvider(cli_tool="claude", model="sonnet")
        text = provider._build_prompt_text([
            {"role": "system", "content": "System."},
            {"role": "user", "content": "Question."},
        ])
        assert "System." in text
        assert "Question." in text

    def test_extract_system_prompt(self):
        provider = CLIProvider(cli_tool="claude", model="sonnet")
        sys, user = provider._extract_system_prompt([
            {"role": "system", "content": "You are a classifier."},
            {"role": "user", "content": "Classify this."},
        ])
        assert sys == "You are a classifier."
        assert user == "Classify this."

    def test_extract_system_prompt_none(self):
        provider = CLIProvider(cli_tool="claude", model="sonnet")
        sys, user = provider._extract_system_prompt([
            {"role": "user", "content": "Just a question."},
        ])
        assert sys is None
        assert user == "Just a question."


# --- Return usage tests ---

class TestReturnUsage:
    @pytest.mark.asyncio
    async def test_structured_with_usage(self):
        envelope = json.dumps({"type": "result", "result": '{"category": "test", "confidence": 0.5}'})
        mock_proc = _make_mock_process(stdout=envelope)

        with patch("doctrail.llm_providers.cli_provider.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            provider = CLIProvider(cli_tool="claude", model="sonnet")
            result, usage = await provider.generate_structured(
                messages=[{"role": "user", "content": "test"}],
                pydantic_model=SimpleResult,
                return_usage=True,
            )

            assert isinstance(result, SimpleResult)
            assert isinstance(usage, TokenUsage)
            assert usage.estimate_cost() == 0.0
            assert "cli/claude/sonnet" in usage.model
