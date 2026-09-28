"""Engine adapters: one `ask(prompt_text, location, options) -> Answer` per engine (SPEC §8)."""

from __future__ import annotations

import os
from typing import Any

from lab.config import EngineSpec
from lab.engines.base import Answer, Citation, EngineError, EngineLike  # noqa: F401 (re-export)


def build_engine(spec: EngineSpec, tunables: dict[str, Any]) -> EngineLike | None:
    """Return a live adapter for the spec, or None when its API keys are missing or the adapter is not built yet."""
    missing = [k for k in spec.env if not os.environ.get(k)]
    if missing:
        raise EngineError(f"engine {spec.id}: missing environment variable(s) {', '.join(missing)}")
    if spec.api == "openai_responses":
        from lab.engines.openai import OpenAIEngine

        return OpenAIEngine.from_spec(spec, tunables, api_key=os.environ[spec.env[0]])
    return None  # gemini / perplexity / dataforseo arrive in slice S2
