# By Machines Lab — PLAN (v0, 2026-09-26)

Technical plan: *how*. Vertical slices, each a complete working path, tests written before code. Product decisions live in `SPEC.md`. Hours are estimates against the build cap (55 h until 18.10).

## Slices

| Slice | Working path | Tests before code | Hours | Target |
|---|---|---|---|---|
| **S1 Panel v0** | YAML panel → OpenAI `web_search` adapter → SQLite → CSV of raw runs. Infra inside the slice: repo, `pyproject`, `.env.example`, logging, config validation. | `test_config_validates_panel` (bad alias, missing class → error) · `test_openai_adapter_parses_fixture` (recorded response → answer text, citations with URL and position, cost) · `test_store_unique_run` (same key twice → one row) · `test_runner_idempotent_same_day` (second run adds 0 rows) | 4–6 | 28–29.09 |
| **S2 Four engines + locations + schedule** | Gemini, Perplexity, DataForSEO adapters; `user_location` where supported; daily runner with resume; budget guard; systemd timer; deploy to the VPS. | `test_gemini_adapter_fixture`, `test_perplexity_adapter_fixture`, `test_dataforseo_adapter_fixture` · `test_location_fallback_when_unsupported` · `test_resume_after_crash` (partial day → re-run completes missing only) · `test_budget_guard_blocks` | 4–6 | 30.09; first live runs Thu 01.10 |
| **S3 Extraction + metrics + export** | Citations → `citations`; firm matching with aliases → `mentions` + `unmatched`; weekly export CSV/JSON with Wilson, shares, Jaccard, our citation. | `test_wilson_reference_values` (3/7, 0/7, 7/7) · `test_jaccard_reference` · `test_source_share_sums_to_one` · `test_alias_matching_normalises_legal_forms` (Lda, S.L., Ltd) · `test_unmatched_queue` · `test_export_schema_columns` | 4–6 | by 07.10 |
| **S4 Census** | Domain list → checks (robots, bot UA fetch, HTML vs JS, WAF, JSON-LD, llms.txt, Bing index via DataForSEO) → `census` table + CSV. | `test_robots_rules_for_ai_bots` (fixture robots.txt) · `test_bot_ua_fetch_status` (mocked HTTP) · `test_jsonld_types_extracted` · `test_html_text_ratio` | 4–6 | script by 07.10, run 08–14.10 |
| **S5 Site** | Hugo site built to the site-architecture document (URL map with reserved sections for articles, interviews, videos, firms, tracks, verticals; `urls.txt` registry; redirects map): pages, lab section `/lab/relocation-europe/` from export JSON, week pages, `/data/` CSVs, RSS per section, sitemap, robots.txt, schema hygiene; nginx on the VPS with logs; Bing WMT + IndexNow. | `test_export_json_renders` (Hugo data template builds with a sample export) · acceptance: `curl -A "OAI-SearchBot"` full text on every page; first retrieval bot visible in nginx logs | 12–16 | 01–17.10 |
| **S6 Firm report v0** | `lab report --domain X` → HTML/PDF from the DB. | `test_report_builds_for_known_firm` · `test_report_top10_instead` | 2–3 | by 02.11 (weekly hours) |

Horizontal steps: none. Server, keys and repository are part of S1/S2 and are named explicitly there.

## Order and gates

S1 → S2 → S3 run on the same code path (runner → store → export); S4 is independent of S2/S3 and can run in parallel once S1's store exists; S5 depends on S3's export schema; S6 depends on S3 and S4.

Definition of done per slice: tests green, one manual critical-path check, a small readable diff, a working state to roll back to (git). Independent review in a fresh context ("find counter-examples") before the slice is called done.

## Before any code (S0, owner + operator, 26–30.09)

1. Owner creates the public repo `bymachines-lab` (MIT) and enables it for the Claude app; orders the VPS; creates API keys (OpenAI, Google AI Studio, Perplexity, DataForSEO).
2. Verify with the docs: location parameters for the four APIs, current model names and prices, DataForSEO minimum deposit.
3. Draft the panel: 50 prompts (16 provider / 12 problem / 8 compare / 4 brand-control / 10 agency), 5 locations for the provider class, seed firm list. Owner reviews ~1 h, panel frozen 30.09.
4. Owner approves `SPEC.md` (answers to §12). 🛑 No code before that.

## Rollback

Every slice is one or a few commits; `git revert` returns to the previous working state. The database is append-only by design; a bad extraction is re-computed from raw JSON, never by editing rows.
