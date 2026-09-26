# By Machines Lab

Open measurement of how AI assistants pick businesses.

By Machines Lab asks a frozen panel of buyer questions to ChatGPT, Gemini, Perplexity and Google AI Mode every day, stores every answer with its citations, and reports which firms get named and how often — with confidence intervals, sources, week-to-week stability and the cost of every run. Zeros are published.

The instrument is vertical-agnostic: a vertical is a config (prompt panel + firm list + locations); the code is shared. Vertical #1: relocation to Europe — immigration lawyers, tax advisors and company-formation agents in Portugal, Spain and Cyprus. Results will appear at https://bymachines.ai/lab/relocation-europe/ from 19 October 2026.

## Status

Spec under review. `SPEC.md` says what and why; `PLAN.md` says how, in vertical slices with tests written before code. No code yet: the first slice starts after the spec is approved.

## What will be here

- `lab/` — panel runner, engine adapters, extraction, metrics, export, census, firm report
- `config/` — engines, the frozen prompt panel per vertical, firm lists with aliases
- `tests/` — offline tests with recorded fixtures and reference values
- `deploy/` — systemd timer and install notes for a small VPS

## What will never be here

API keys, the raw-answer database, firm contacts. Keys go in `.env` (see `.env.example`); data lives in `data/`, which is git-ignored. Weekly exports are published on the site.

## Method in one paragraph

Repeated runs — seven per prompt, engine and location per week — because AI answers are non-deterministic. Mention *frequency* with a confidence interval, never position. Engines read separately. API runs for the weekly dynamics, a manual UI control on a few prompts for calibration. Every reading is pre-registered before its data arrive. Details: `SPEC.md` §1–§2 and §9–§10.

## License

Code: MIT, see `LICENSE`. Data exports on the site: CC BY 4.0 (proposed).
