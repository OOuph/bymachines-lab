"""`.env` loading edge cases (review S1): inline comments, `export`, empty exported values."""

from __future__ import annotations

import os

from lab.env import load_dotenv


def test_dotenv_inline_comment_export_and_empty_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# keys\nOPENAI_API_KEY='sk-test' # my key\nexport PERPLEXITY_API_KEY=pplx-1\nGEMINI_API_KEY=\nDATAFORSEO_LOGIN=login#notacomment\n")
    for k in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "GEMINI_API_KEY", "DATAFORSEO_LOGIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "")   # empty exported value must not shadow the file
    assert load_dotenv(env) == env
    assert os.environ["OPENAI_API_KEY"] == "sk-test"
    assert os.environ["PERPLEXITY_API_KEY"] == "pplx-1"
    assert os.environ.get("GEMINI_API_KEY", "") == ""
    assert os.environ["DATAFORSEO_LOGIN"] == "login#notacomment"


def test_dotenv_does_not_override_real_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=from-file\n")
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    load_dotenv(env)
    assert os.environ["OPENAI_API_KEY"] == "from-shell"
