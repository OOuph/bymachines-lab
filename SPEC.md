# By Machines Lab — SPEC (v0, 2026-09-26)

**Status:** draft for owner approval (BPS phase 3). No code is written until the owner approves this file. Product spec only: *what* and *why*. The technical plan (*how*, vertical slices, tests) is in `PLAN.md`.

## 1. Goal and users

By Machines Lab measures how AI assistants — ChatGPT, Gemini, Perplexity, Google AI Mode / AI Overviews — name businesses when a buyer asks. It is an open instrument: a frozen prompt panel, repeated runs, every answer stored with its citations, mention frequency with confidence intervals, and the code to reproduce it. Zeros are published.

The instrument is vertical-agnostic. A **vertical is a config** (prompt panel + firm list + locations); the code is shared. Vertical #1: **relocation to Europe** — immigration lawyers, tax advisors, company-formation agents in Portugal, Spain and Cyprus. Series name on the site: **Relocation Lab**, path `/lab/relocation-europe/`.

Users:
- **Operator** (one person): runs the weekly panel, reads exports, writes lab posts, produces firm reports.
- **Readers** of bymachines.ai: firms in the vertical and AI-search practitioners who consume tables and CSVs.
- **Reproducers**: anyone who clones the repo, adds their own API keys and re-runs the panel (MIT).

Why open: results are trustworthy only if the panel, the method and the code can be inspected and re-run.

## 2. Scenarios and acceptance criteria

| # | Scenario | Acceptance criteria |
|---|---|---|
| S-A | **Daily panel run.** Every day the runner asks each prompt of the panel to each engine (in each configured location for the `provider` class), one run per day, seven runs per ISO week. | One row per `(iso_week, prompt_id, engine_id, location, run_idx)`; raw JSON, answer text, citations and cost stored; re-running the same day adds nothing; a crash mid-run followed by a re-run completes only the missing rows. |
| S-B | **Extraction.** Citations come from API metadata (URL, position). Firm mentions are matched against a frozen firm list with aliases; unknown proper names go to an `unmatched` queue for weekly manual review. | Reference fixtures (§10) pass; on 20 hand-labelled baseline answers, automatic firm matching agrees with the operator on ≥90 % of mentions. |
| S-C | **Weekly export.** For each ISO week: mention frequency per firm × engine × location with Wilson 95 % interval, source share by domain and source type, week-to-week stability (Jaccard), `bymachines.ai` citation count, cost. CSV and JSON. | Files exist for every completed week; values reproduce §10; JSON carries `schema_version`; the site build reads them without edits. |
| S-D | **Census.** For every firm in the list and a control sample: robots.txt rules for AI bots, HTTP status under bot user agents, text present in initial HTML vs JS-only, CDN/WAF signature, JSON-LD types, `llms.txt` presence, LinkedIn page and followers (manual field), directory presence, Bing-indexed pages, language versions. | On 10 known domains (5 behind Cloudflare, 5 not) results match a manual check; a full run over 60–160 domains finishes in under 30 minutes; output is a table joinable to firms. |
| S-E | **Site data.** The static site (Hugo) renders the weekly tables from the export JSON with a CSV download and the method version. URL map, sections, taxonomies and the content model are fixed in the site-architecture document (kept with the site; to be published as `docs/site-architecture.md`) before S5; addresses never change. | One command builds the site from the latest export; `curl -A "OAI-SearchBot"` and `curl -A "PerplexityBot"` return the full text of every page. |
| S-F | **Firm report — "Does AI see you?"** One command, one domain → HTML/PDF: frequency by engine and location over the last 4 weeks, who is named instead (top 10 on the same prompts), sources, the firm's census row, 3–5 observations, no promises. | Report for any firm in the DB builds in under 1 minute and reads without explanation. |
| S-G | **Budget guard.** Before each daily run the projected weekly cost is computed; if it exceeds the cap the run stops and an alert is written. | With cap $50 and a simulated overrun the run does not start, `data/ALERT` exists, the log says why. |
| S-H | **Vertical portability.** A new vertical = new panel YAML + firm list + locations. | Bringing up a second vertical requires zero code changes and ≤4 hours of operator work; the first vertical's panel stays frozen. |

## 3. Non-goals

No dashboard or SaaS; no multi-tenant; no scraping of consumer UIs (reserve: Elmo / GetCito on week 8 if manual UI control is too costly); no Choose / Pay tracks; no `llms.txt` generation; no ranking or position metrics; no editorial judgement of firm quality — the lab reports whom the engines name, not who is better; no languages other than English; no local business profiles.

## 4. Errors and edge cases

- **API failure**: 3 retries with exponential backoff; then `status=error` with the message stored; other engines and prompts continue; missing rows are picked up by the next run in the same week.
- **Missing key**: the engine is skipped with a warning at start; the run proceeds for the others.
- **Engine without a location parameter**: the engine is queried once per prompt; `engines.supports_location=0`; exports show a single location for it.
- **Answer without citations**: stored and flagged; counted in "answers without citations" per engine.
- **Alias collision** (two firms sharing an alias): the alias is rejected at config validation; the run does not start until fixed.
- **Same firm in two countries**: one firm record per legal entity with a `country` field.
- **Week boundary**: ISO week in UTC; the daily run is scheduled at 06:00 UTC.
- **Crash mid-run**: the database is the checkpoint; re-run is idempotent.
- **Model renamed or deprecated**: a preflight call per engine at start; failure surfaces as a config error before any spend.
- **Rate limits**: concurrency ≤4, per-engine backoff on 429.
- **Cost overrun**: budget guard (S-G).

## 5. Permissions and security

- API keys only in environment variables; the repo ships `.env.example`, never `.env`.
- The database (raw answers) and logs live in `data/`, git-ignored, backed up weekly by rsync to the operator's machine; not published.
- Published artefacts: weekly exports (aggregates and firm names as the engines say them, which are public AI answers) and the code.
- The public repo contains no personal data and no firm contacts; the firm list holds names, domains, aliases and country only.
- Bots we run identify themselves with a descriptive user agent for the census; robots.txt is read, not bypassed.

## 6. Reuse and contracts that must not break

New project, nothing reused. Contracts that later code and the site depend on:
- SQLite schema (tables `prompts`, `engines`, `runs`, `citations`, `brands`, `mentions`, `costs`, `census`, `ui_control`); unique key on `runs` as in S-A.
- Export schema: `brand_frequency.csv` (`brand, engine, location, n_runs, n_mentioned, freq, wilson_low, wilson_high`), `sources.csv` (`engine, domain, source_type, share`), `stability.csv` (`engine, jaccard_vs_prev_week`), `our_citation.csv` (`prompt_id, engine, n_runs, n_cited`), `costs.csv` (`engine, calls, cost_usd`), plus the same in JSON with `schema_version`.
- Config schema: `config/panel.<vertical>.yaml`, `config/firms.<vertical>.yaml`, `config/engines.yaml`.
- CLI: `lab run`, `lab export`, `lab census`, `lab report`, `lab status`.

## 7. End-to-end verification

1. `pytest` offline with recorded fixtures for every engine adapter, the extractor, the metrics and the exporter.
2. `lab run --limit 5 --runs 1` against live APIs: 5 prompts × 4 engines × 1 run, total cost under $1, rows with citations and costs present, log readable.
3. `lab export --week <iso>` produces the files of §6; the site builds from them.
4. Re-run the same day → row count unchanged. Kill a run mid-way, re-run → only missing rows added.
5. Budget guard simulated with cap $0.01 → run refuses to start, alert written.

## 8. Implementation (spec → code mapping)

- Package `lab/`: `config.py` (YAML load and validation), `engines/openai.py`, `engines/gemini.py`, `engines/perplexity.py`, `engines/dataforseo.py` (one function each: `ask(prompt, location) -> Answer`), `store.py` (SQLite), `runner.py` (plans the day's runs, idempotent), `extract.py` (citations, mentions, unmatched queue), `metrics.py` (Wilson, Jaccard, shares), `export.py`, `census.py`, `report.py`, `cli.py`.
- Config: `config/engines.yaml` (models, modes, prices ⚠ to verify), `config/panel.relocation-europe.yaml` (prompts with class and locations), `config/firms.relocation-europe.yaml` (canonical name, aliases, country, type, website).
- Tunables at the top of `config/engines.yaml`: `runs_per_week: 7`, `weekly_budget_usd: 50`, `concurrency: 4`, `retries: 3`, `daily_hour_utc: 6`.
- Data: `data/lab.sqlite`, `data/logs/YYYY-MM-DD.log`, `data/export/<iso_week>/`, `data/ALERT`.
- Deployment: `deploy/lab.service` + `deploy/lab.timer` (systemd, daily 06:00 UTC) on the VPS; `deploy/README.md` with the install steps.
- Logging: levels with timestamps to stdout and to the daily log file.
- Python 3.12, dependencies kept small (`httpx`, `pyyaml`, `pytest`; provider SDKs only where the raw HTTP API is awkward).

## 9. Expected ranges (funnel)

Calls per week: 3 192 (2 240 provider-class × 5 locations + 672 other classes + 280 agency mini-panel) — fixed while the panel is frozen. Answers with ≥1 citation: 60–95 % per engine (below 40 % → check the tool parameters). Firms named during baseline: 30–80 (below 10 → prompts too generic or extraction broken; above 200 → alias duplication). Weekly cost: $20–46 (above $50 → guard, then the provider class drops to 3 locations). Unmatched names after week 2: below 15 % of mentions.

## 10. Reference values (tests must reproduce)

- Wilson 95 % interval: 3 of 7 → 0.43 [0.16; 0.75]; 0 of 7 → 0.00 [0.00; 0.35]; 7 of 7 → 1.00 [0.65; 1.00].
- Jaccard stability: {A, B, C} vs {B, C, D} → 0.50.
- Source share: sums to 1.0 per engine-week.
- Our citation: count of citations whose host is `bymachines.ai` or `www.bymachines.ai`; text mentions do not count.

## 11. Assumptions to challenge

1. Relocating buyers actually ask AI assistants for lawyers and tax advisors — not measured; the baseline is the first evidence.
2. One run per day for seven days ≈ seven independent runs — to be checked for day-of-week autocorrelation in the "Noise" post.
3. API answers are a usable proxy for consumer UI answers — known to be weak (≈24 % brand overlap for ChatGPT ⚠); mitigated by a weekly manual UI control of 5–10 prompts recorded in `ui_control`.
4. The five locations for the provider class (Lisbon, Madrid, Limassol, London, New York) are the right split between destinations and origins.
5. DataForSEO's AI Mode endpoint returns citations comparable to the consumer surface.
6. A frozen panel for 12 weeks beats an evolving one — accepted as a method rule; new prompts go to a `v2` class with its own start date.

## 12. Open questions for the owner

1. **Model tier.** Weekly panel on the current flagship non-reasoning model (closest to the consumer product, higher cost) or on the mini tier (≈3× cheaper) — or flagship for the provider class only?
2. **Publish the panel before launch?** The repo is public from the first commit; the frozen 50 prompts and the firm list would be visible before the site opens on 19.10. Publish from day one (open method) or keep `config/` private until launch?
3. **Site source.** Hugo site in the same public repo (posts visible before publication) or a separate private `bymachines-site` repo?
