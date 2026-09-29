"""Engine adapters: one `ask(prompt_text, location, options) -> Answer` per engine (SPEC §8)."""

from __future__ import annotations

import os
from typing import Any

from lab.config import EngineSpec
from lab.engines.base import Answer, Citation, EngineError, EngineLike  # noqa: F401 (re-export)


def build_engine(spec: EngineSpec, tunables: dict[str, Any]) -> EngineLike | None:
    """Return a live adapter for the spec; raise EngineError when its keys are missing; None for an unknown api."""
    missing = [k for k in spec.env if not os.environ.get(k)]
    if missing:
        raise EngineError(f"engine {spec.id}: missing environment variable(s) {', '.join(missing)}")
    env = {k: os.environ[k] for k in spec.env}
    if spec.api == "openai_responses":
        from lab.engines.openai import OpenAIEngine

        return OpenAIEngine.from_spec(spec, tunables, api_key=env[spec.env[0]])
    if spec.api == "perplexity_agent":
        from lab.engines.perplexity import PerplexityEngine

        return PerplexityEngine.from_spec(spec, tunables, api_key=env[spec.env[0]])
    if spec.api == "gemini_generate_content":
        from lab.engines.gemini import GeminiEngine

        return GeminiEngine.from_spec(spec, tunables, api_key=env[spec.env[0]])
    if spec.api == "dataforseo_ai_mode":
        from lab.engines.dataforseo import DataForSEOEngine

        login = env.get("DATAFORSEO_LOGIN") or env[spec.env[0]]
        password = env.get("DATAFORSEO_PASSWORD") or env[spec.env[-1]]
        return DataForSEOEngine.from_spec(spec, tunables, login=login, password=password)
    return None
