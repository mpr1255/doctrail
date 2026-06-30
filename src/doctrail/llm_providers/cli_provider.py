"""CLI-based LLM provider using claude, gemini, and codex command-line tools.

Routes models by prefix:
    cli/claude/sonnet     → claude -p --model sonnet
    cli/claude/opus       → claude -p --model opus
    cli/gemini/flash      → gemini -p -m gemini-2.5-flash
    cli/codex/gpt-5.5    → codex exec -m gpt-5.5

These CLIs are subscription-based, so per-token cost is $0.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Dict, Any, Type, Optional, List, Tuple, Union

from pydantic import BaseModel
from ..utils.model_pricing import canonicalize_model_name

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token usage from CLI providers (estimated, since CLIs don't always report)."""
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimate_cost(self) -> float:
        """CLI tools are subscription-based — cost is $0."""
        return 0.0


# Model aliases for each CLI tool
CLAUDE_ALIASES = {
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
    "claude-sonnet-4": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4": "opus",
    "claude-opus-4-6": "opus",
    "claude-haiku-4-5": "haiku",
}

# Default models when none specified
CLI_DEFAULTS = {
    "claude": "sonnet",
    "gemini": "gemini-2.5-flash",
    "codex": "gpt-5.5",
}

# Subprocess timeout (seconds)
DEFAULT_TIMEOUT = 300


def _extract_json_from_text(text: str) -> dict:
    """Extract a JSON object from text that may contain markdown or preamble."""
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

    raise ValueError(f"Could not extract JSON from CLI response: {text[:300]}")


async def _run_subprocess(cmd: list, stdin_text: str = None, timeout: int = DEFAULT_TIMEOUT,
                          env: dict = None) -> Tuple[str, str, int]:
    """Run a subprocess asynchronously and return (stdout, stderr, returncode).

    Uses asyncio.create_subprocess_exec for true async — does NOT block the event loop.
    """
    merged_env = dict(os.environ)
    if env:
        for k, v in env.items():
            if v is None:
                merged_env.pop(k, None)
            else:
                merged_env[k] = v

    logger.debug(f"CLI subprocess: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_text else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=merged_env,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"CLI subprocess timed out after {timeout}s: {' '.join(cmd[:3])}")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, proc.returncode


class CLIProvider:
    """LLM provider that calls claude/gemini/codex CLIs as subprocesses.

    Usage:
        provider = CLIProvider(cli_tool="claude", model="sonnet")
        result = await provider.generate_structured(messages, MyModel)
    """

    def __init__(self, cli_tool: str, model: str, timeout: int = DEFAULT_TIMEOUT):
        """
        Args:
            cli_tool: One of "claude", "gemini", "codex"
            model: Model name/alias to pass to the CLI
            timeout: Subprocess timeout in seconds
        """
        if cli_tool not in ("claude", "gemini", "codex"):
            raise ValueError(f"Unknown CLI tool: {cli_tool}. Must be claude, gemini, or codex.")

        self.cli_tool = cli_tool
        if cli_tool == "claude":
            raw_model = model or CLI_DEFAULTS.get(cli_tool, "")
            self.model = canonicalize_model_name(
                f"cli/claude/{raw_model}"
            ).removeprefix("cli/claude/")
        else:
            self.model = model or CLI_DEFAULTS.get(cli_tool, "")
        self.timeout = timeout

        self.context_limits = {
            "claude": 200000,
            "gemini": 1000000,
            "codex": 200000,
        }

    def _build_prompt_text(self, messages: List[Dict[str, str]]) -> str:
        """Convert OpenAI-style messages into a single prompt string for CLI stdin."""
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(content)
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
            else:
                parts.append(content)
        return "\n\n".join(parts)

    def _extract_system_prompt(self, messages: List[Dict[str, str]]) -> Tuple[Optional[str], str]:
        """Separate system prompt from user content.

        Returns (system_prompt_or_None, user_prompt_text).
        """
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

    # ── Claude CLI ────────────────────────────────────────────────────

    async def _call_claude(self, prompt: str, system_prompt: str = None) -> str:
        """Call claude -p with a minimal system prompt.

        Key design decisions:
        - --system-prompt replaces the massive Claude Code default (~90K tokens → ~10 tokens).
          Without this, every call takes 10+ seconds just loading context.
        - We do NOT use --json-schema: it uses internal tool calls requiring 2+ turns
          and returns result=null. Instead, schema is injected into the system prompt
          (same approach as gemini) and JSON is parsed from the text response.
        - --max-turns 1: single turn, no agentic looping.
        - --allowedTools "": disables all tools (Read, Edit, Bash, etc.)
        - --output-format json: returns envelope with .result containing the model text.
        """
        model = CLAUDE_ALIASES.get(self.model, self.model)

        cmd = [
            "claude", "-p",
            "--model", model,
            "--output-format", "json",
            "--max-turns", "1",
            "--no-session-persistence",
            "--allowedTools", "",
            "--system-prompt", system_prompt or "",
        ]

        # Must unset CLAUDECODE to avoid "cannot run nested" error
        env = {"CLAUDECODE": None}

        stdout, stderr, rc = await _run_subprocess(cmd, stdin_text=prompt,
                                                    timeout=self.timeout, env=env)
        if rc != 0:
            raise RuntimeError(f"claude CLI exited {rc}: {stderr[:500]}")

        # claude --output-format json wraps the result in a JSON envelope:
        # {"type": "result", "result": "...the actual text...", "usage": {...}}
        try:
            envelope = json.loads(stdout)
            if isinstance(envelope, dict) and "result" in envelope:
                return envelope["result"] or ""
            return stdout
        except json.JSONDecodeError:
            return stdout

    # ── Gemini CLI ────────────────────────────────────────────────────

    async def _call_gemini(self, prompt: str) -> str:
        """Call gemini -p in non-interactive mode.

        Key flags:
        - -p: Non-interactive (headless) mode.
        - -e none: Disable all extensions (no file read/write, no shell, etc.)
        - -o text: Plain text output (no JSON envelope, unlike claude).
        - --approval-mode plan: Read-only, extra safety.
        """
        cmd = [
            "gemini",
            "-p", prompt,
            "-m", self.model,
            "-e", "none",
            "--approval-mode", "plan",
        ]

        stdout, stderr, rc = await _run_subprocess(cmd, timeout=self.timeout)
        if rc != 0:
            raise RuntimeError(f"gemini CLI exited {rc}: {stderr[:500]}")

        return stdout

    # ── Codex CLI ─────────────────────────────────────────────────────

    async def _call_codex(self, prompt: str, schema_json: str = None) -> str:
        """Call codex exec in non-interactive mode.

        Key flags:
        - exec: Non-interactive subcommand.
        - --sandbox read-only: No file writes.
        - --ignore-user-config / --ignore-rules: avoid local personality,
          project rules, MCP servers, and plugins for classifier-style calls.
        - --ephemeral: No session files written to disk.
        - --output-schema: JSON schema file for structured output validation.
        - -o: Write final message to file (more reliable than stdout parsing).
        - --skip-git-repo-check: Don't require a git repo.
        - --disable overrides: remove interactive/browser/shell tool surfaces.
        - -c overrides: set low reasoning and no personality.
        """
        cmd = [
            "codex", "exec",
            "-m", self.model,
            "--sandbox", "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C", "/tmp",   # neutral working dir — avoids loading project context (~1.7K tokens)
            # Classifier mode: no interactive tools, no browser/computer-use,
            # no plugins/apps/memory/multi-agent surfaces, low reasoning,
            # and no personality. Structured output is enforced below with
            # --output-schema and -o.
            "--disable", "shell_tool",
            "--disable", "browser_use",
            "--disable", "browser_use_external",
            "--disable", "computer_use",
            "--disable", "image_generation",
            "--disable", "tool_search",
            "--disable", "apps",
            "--disable", "plugins",
            "--disable", "memories",
            "--disable", "multi_agent",
            "-c", 'model_reasoning_effort="low"',
            "-c", 'personality="none"',
        ]

        # Codex requires schema in a file, not inline
        schema_file = None
        output_file = None
        try:
            if schema_json:
                schema_file = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.json', delete=False
                )
                # Codex requires additionalProperties: false on all objects
                schema_data = json.loads(schema_json)
                _add_additional_properties_false(schema_data)
                schema_file.write(json.dumps(schema_data))
                schema_file.close()
                cmd.extend(["--output-schema", schema_file.name])

                output_file = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.json', delete=False
                )
                output_file.close()
                cmd.extend(["-o", output_file.name])

            # Pass prompt via stdin to avoid shell ARG_MAX limits with large/CJK text
            stdout, stderr, rc = await _run_subprocess(cmd, stdin_text=prompt, timeout=self.timeout)
            if rc != 0:
                raise RuntimeError(f"codex CLI exited {rc}: {stderr[:500]}")

            # If we used output file, read from there (more reliable)
            if output_file and os.path.exists(output_file.name):
                with open(output_file.name) as f:
                    content = f.read().strip()
                if content:
                    return content

            return stdout

        finally:
            if schema_file:
                try:
                    os.unlink(schema_file.name)
                except OSError:
                    pass
            if output_file:
                try:
                    os.unlink(output_file.name)
                except OSError:
                    pass

    # ── Public interface (matches LLMProvider protocol) ───────────────

    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        return_usage: bool = False,
    ) -> Union[BaseModel, Tuple[BaseModel, Optional[TokenUsage]]]:
        """Generate structured output via CLI subprocess."""
        logger.debug(f"CLI structured output: tool={self.cli_tool}, model={self.model}, "
                     f"schema={pydantic_model.__name__}")

        schema = pydantic_model.model_json_schema()
        schema_json = json.dumps(schema)
        system_prompt, prompt = self._extract_system_prompt(messages)

        # Build schema instruction — injected into prompt for all CLI tools
        schema_instruction = (
            "You MUST respond with ONLY a valid JSON object matching this schema. "
            "No markdown, no explanation, no text before or after the JSON.\n"
            f"Schema: {schema_json}"
        )

        if self.cli_tool == "claude":
            # Claude: inject schema into --system-prompt, keep user prompt clean
            combined_system = f"{system_prompt}\n\n{schema_instruction}" if system_prompt else schema_instruction
            raw = await self._call_claude(prompt, system_prompt=combined_system)
        elif self.cli_tool == "gemini":
            # Gemini: no --system-prompt flag, so append schema to user prompt
            if system_prompt:
                prompt = f"{system_prompt}\n\n{prompt}"
            prompt = f"{prompt}\n\n{schema_instruction}"
            raw = await self._call_gemini(prompt)
        elif self.cli_tool == "codex":
            raw = await self._call_codex(prompt, schema_json=schema_json)
        else:
            raise ValueError(f"Unknown CLI tool: {self.cli_tool}")

        # Parse the response
        data = _extract_json_from_text(raw)
        result = pydantic_model(**data)

        usage = TokenUsage(
            input_tokens=len(prompt) // 4,
            output_tokens=len(raw) // 4,
            model=f"cli/{self.cli_tool}/{self.model}",
        )

        logger.debug(f"CLI structured output success: {type(result)}")
        if return_usage:
            return result, usage
        return result

    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate unstructured text via CLI subprocess."""
        system_prompt, prompt = self._extract_system_prompt(messages)

        if self.cli_tool == "claude":
            return await self._call_claude(prompt, system_prompt=system_prompt)
        elif self.cli_tool == "gemini":
            # Prepend system prompt to user prompt for gemini
            if system_prompt:
                prompt = f"{system_prompt}\n\n{prompt}"
            return await self._call_gemini(prompt)
        elif self.cli_tool == "codex":
            return await self._call_codex(prompt)
        else:
            raise ValueError(f"Unknown CLI tool: {self.cli_tool}")

    def count_tokens(self, text: str) -> int:
        """Approximate token count (~4 chars per token)."""
        return len(text) // 4

    @property
    def max_context_tokens(self) -> int:
        """Maximum context window for the CLI tool."""
        return self.context_limits.get(self.cli_tool, 200000)


def _add_additional_properties_false(schema: dict) -> None:
    """Recursively add additionalProperties: false to all objects in a JSON schema.

    Required by Codex's --output-schema (OpenAI structured outputs requirement).
    """
    if not isinstance(schema, dict):
        return

    if schema.get("type") == "object":
        schema["additionalProperties"] = False

    for key in ("properties", "items", "$defs", "definitions"):
        val = schema.get(key)
        if isinstance(val, dict):
            for sub in val.values():
                _add_additional_properties_false(sub)
        elif isinstance(val, list):
            for sub in val:
                _add_additional_properties_false(sub)

    # Handle anyOf, oneOf, allOf
    for key in ("anyOf", "oneOf", "allOf"):
        val = schema.get(key)
        if isinstance(val, list):
            for sub in val:
                _add_additional_properties_false(sub)
