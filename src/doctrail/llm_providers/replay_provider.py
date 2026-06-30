"""Offline replay provider for tutorial and fixture-backed tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from pydantic import BaseModel

from .openai_provider import TokenUsage


class ReplayProvider:
    """Return canned structured responses from .doctrail/replay/*.jsonl."""

    uses_replay_fixtures = True

    def __init__(
        self,
        model: str = "replay",
        *,
        enrichment_name: Optional[str] = None,
        replay_dir: Optional[Path] = None,
    ):
        self.model = model
        self.label = model.split("/", 1)[1] if "/" in model else "default"
        self.enrichment_name = enrichment_name
        self.replay_dir = replay_dir or (Path.cwd() / ".doctrail" / "replay")
        self._fixtures: Dict[str, Dict[Tuple[str, str], Any]] = {}

    def supports_reasoning_effort(self) -> bool:
        return False

    @staticmethod
    def _model_name_to_enrichment_name(pydantic_model: Type[BaseModel]) -> str:
        model_name = pydantic_model.__name__
        if model_name.endswith("Model"):
            model_name = model_name[:-5]
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", model_name).lower()
        return name or "unknown"

    def _fixture_path(self, enrichment_name: str) -> Path:
        return self.replay_dir / f"{enrichment_name}.jsonl"

    def _load_fixtures(self, enrichment_name: str) -> Dict[Tuple[str, str], Any]:
        if enrichment_name in self._fixtures:
            return self._fixtures[enrichment_name]

        fixture_path = self._fixture_path(enrichment_name)
        if not fixture_path.exists():
            raise FileNotFoundError(
                f"Replay fixture not found: {fixture_path}"
            )

        fixtures: Dict[Tuple[str, str], Any] = {}
        with fixture_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in replay fixture {fixture_path}:{line_number}: {exc}"
                    ) from exc

                if "key_value" not in row:
                    raise ValueError(
                        f"Replay fixture {fixture_path}:{line_number} is missing key_value"
                    )
                if "response" not in row:
                    raise ValueError(
                        f"Replay fixture {fixture_path}:{line_number} is missing response"
                    )

                label = str(row.get("label") or row.get("model_label") or row.get("model") or "default")
                response = row["response"]
                if isinstance(response, str):
                    response = json.loads(response)
                fixtures[(str(row["key_value"]), label)] = response

        self._fixtures[enrichment_name] = fixtures
        return fixtures

    def available_labels(self, enrichment_name: str) -> List[str]:
        """Return labels present in the fixture for this enrichment."""
        fixtures = self._load_fixtures(enrichment_name)
        return sorted({label for _, label in fixtures})

    def preflight_enrichment(self, enrichment_name: Optional[str] = None) -> None:
        """Fail before row processing when replay fixtures lack the requested label."""
        name = enrichment_name or self.enrichment_name
        if not name:
            return

        labels = self.available_labels(name)
        if labels and self.label not in labels:
            fixture_path = self._fixture_path(name)
            available = ", ".join(f"replay/{label}" for label in labels)
            raise ValueError(
                f"Replay fixture {fixture_path} has no entries for label {self.label!r}. "
                f"Available labels: {available}. "
                f"Rerun with --model replay/<label>, for example --model replay/{labels[0]}."
            )

    async def generate_structured(
        self,
        messages: List[Dict[str, str]],
        pydantic_model: Type[BaseModel],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        return_usage: bool = False,
        replay_key_value: Optional[Any] = None,
        **_: Any,
    ) -> Union[BaseModel, Tuple[BaseModel, TokenUsage]]:
        enrichment_name = self.enrichment_name or self._model_name_to_enrichment_name(pydantic_model)
        fixture_path = self._fixture_path(enrichment_name)

        if replay_key_value is None:
            raise ValueError(
                f"Replay provider needs a key_value for fixture lookup: {fixture_path}"
            )

        key = (str(replay_key_value), self.label)
        fixtures = self._load_fixtures(enrichment_name)
        self.preflight_enrichment(enrichment_name)
        if key not in fixtures:
            raise KeyError(
                f"No replay response in {fixture_path} for key_value={replay_key_value!r}, label={self.label!r}"
            )

        result = pydantic_model(**fixtures[key])
        usage = TokenUsage(input_tokens=0, output_tokens=0, model=self.model)
        if return_usage:
            return result, usage
        return result

    async def generate_text(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        **_: Any,
    ) -> str:
        raise NotImplementedError("ReplayProvider only supports structured output fixtures.")

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    @property
    def max_context_tokens(self) -> int:
        return 1_000_000
