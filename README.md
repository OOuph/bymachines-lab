# By Machines Lab

Open measurement of how AI assistants pick businesses.

By Machines Lab asks a frozen panel of buyer questions to ChatGPT, Gemini, Perplexity and Google AI Mode every day, stores every answer with its citations, and reports which firms get named and how often — with confidence intervals, sources, week-to-week stability and the cost of every run. Zeros are published.

The instrument is vertical-agnostic: a vertical is a config (prompt panel + firm list + locations); the code is shared. Vertical #1: relocation to Europe — immigration lawyers, tax advisors and company-formation agents in Portugal, Spain and Cyprus. Results are published at https://bymachines.ai/lab/relocation-europe/ since 29 September 2026 and refreshed after every daily run.

## Status

`SPEC.md` v1 approved by the owner on 2026-09-28; `PLAN.md` says how, in vertical slices with tests written before code. **S1 (panel v0)**, **S2 (four engines, catch-up window, budget guard, systemd schedule, VPS deploy)** and **S3 (extraction, metrics, weekly export)** are built: config validation, the freeze journal, adapters for OpenAI Responses `web_search`, Perplexity Agent API (`perplexity/sonar`), Gemini `generateContent` grounding and DataForSEO Google AI Mode, the SQLite store, an idempotent day planner and runner, alias matching with an unmatched queue, Wilson / Jaccard / source-share metrics, CSV + JSON exports, `lab validate | freeze | run | status | extract | export | unmatched`, `deploy/` for a small VPS. S4 (census) follows. This build runs without any online publication.

```bash
uv sync                                   # Python 3.12, httpx, pyyaml, pytest
uv run pytest                             # offline tests with recorded fixtures
uv run lab validate                       # load and check the vertical config
uv run lab freeze --panel config/panels/relocation-europe.yaml   # record the sha256 in config/panel-hashes.txt
uv run lab run --dry-run                  # today's plan and its cost estimate (refuses unfrozen panels)
uv run lab run --allow-unfrozen --db data/smoke.sqlite --limit 5 --runs 2 --engine openai --location lisbon --panel human
uv run lab status --db data/smoke.sqlite
uv run lab extract --db data/smoke.sqlite                    # citations → mentions / unmatched with the current firm list (re-runnable)
uv run lab unmatched --db data/smoke.sqlite                  # weekly review queue under the current rules → grow config/firms/<vertical>.yaml
uv run lab export --db data/smoke.sqlite --week 2026-W40     # re-extracts the week (+ previous) and writes the SPEC §6 tables, schema 2 (and runs.csv); --no-extract to skip
uv run python deploy/provision_do.py      # DigitalOcean droplet + firewall + daily backups (see deploy/README.md for install and sync)
uv run python deploy/backup_to_mac.py --install   # daily verified copy of the database to this Mac (launchd); --status to check
```

## What is here

- `lab/` — panel runner, engine adapters, extraction, metrics, export, census, firm report
- `config/` — engines, the frozen prompt panel per vertical, firm lists with aliases
- `tests/` — offline tests with recorded fixtures and reference values
- `deploy/` — systemd timer and install notes for a small VPS

## What will never be here

API keys, the raw-answer database, firm contacts. Keys go in `.env` (see `.env.example`); data lives in `data/`, which is git-ignored; the server's `data/` is backed up daily (`deploy/README.md` §3). Weekly exports are published on the site.

## Method in one paragraph

Repeated runs — seven per prompt, engine and location per week — because AI answers are non-deterministic. Mention *frequency* with a confidence interval, never position. Engines read separately. API runs for the weekly dynamics, a manual UI control on a few prompts for calibration. Every reading is pre-registered before its data arrive. Details: `SPEC.md` §1–§2 and §9–§10.

## License

Code: MIT, see `LICENSE`. Data exports on the site: CC BY 4.0 (proposed).
