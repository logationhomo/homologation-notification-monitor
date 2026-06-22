# Handover — Notification Monitor

_Last updated: 2026-06-22. Read this first when resuming._

## How to resume in a new session (token-efficient)

A new chat has no memory of this one. To pick up efficiently:

1. **Upload the project** at the start of the next session — either the repo
   downloaded as a zip, or the latest project zip. Claude reads files directly
   from the upload; no pasting code (far cheaper on tokens).
2. **Upload this `handover.md`** alongside it — it carries all decisions and
   context so nothing is re-derived.
3. **For a coding-heavy session**, prefer a setup that reads the GitHub repo
   directly (e.g. Claude Code or a repo connection) so edits map straight back.
4. Keep `handover.md` committed in the repo so it stays versioned with the code.

---

## What this project is

An automated monitor that checks web data sources on a daily schedule, detects
**new entries** since the last check, and publishes a clean HTML report. First
(and currently only) source: **ePing** (WTO TBT/SPS trade notifications),
filtered for GCC + Morocco vehicle regulations (ICS code 43.020).

**Audience:** non-technical viewers who open one bookmarked link and read what's
new, each with a link to the official source and an AI relevance note.

## Architecture (and why)

- **Runner:** GitHub Actions, daily cron + manual trigger. Free, no server.
- **State:** a committed JSON file (`state/state.json`) — Git gives change
  history; chosen over SQLite because state is tiny and Git-diffable.
- **Fetching:** ePing exposes a JSON API (`/api/v1/azureSearch/getAll`), so no
  browser is needed for it. Playwright is stubbed for future JS-only sources.
- **Summaries:** Google Gemini (free tier), optional and fails soft.
- **Delivery:** GitHub Pages serves `docs/report.html` at a fixed URL.

## Current status: WORKING

End-to-end pipeline confirmed working on GitHub Actions:
fetch → detect new → Gemini relevance note → report → commit state → Pages.
The latest run produced correct multi-sentence relevance notes.

## Repo file map

```
monitor.py            engine: fetch -> diff -> summarize -> render -> save state
sites.json            what to monitor (ePing GCC/43.020 configured)
fetchers.py           per-source fetchers: eping_api (live), playwright_table (stub)
extractors.py         normalises raw records to a uniform entry shape
summarizer.py         Gemini relevance note: model fallback chain, fails soft
report_template.html  Jinja2 report layout
local_test.py         offline test against a saved real ePing API response
state/state.json      seen-item memory (committed; do NOT delete)
docs/                 published report (GitHub Pages serves this)
.github/workflows/monitor.yml   daily automation + diagnostics + Pages deploy
README.md             setup + how to add sources + how non-technical users read it
handover.md           this file
```

Files touched during the build/debug arc: `summarizer.py`, `monitor.py`,
`monitor.yml`, `README.md`. Unchanged since first build: `fetchers.py`,
`extractors.py`, `sites.json`, `report_template.html`.

## How detection works

Each ePing notification has a stable `id`. The state file stores seen `id`s per
site. New = any `id` not previously seen. **First run for a source is a
baseline** (records current ids, flags nothing) so you don't get a day-one flood.
`track_limit` (currently 30) caps how many recent items are tracked per source.

## Debugging arc (problems hit and how they were resolved)

1. **ePing page is JS-rendered** (Vue placeholders in raw HTML). Resolved by
   finding the underlying JSON API via browser Network tab → no browser needed.
2. **Git push rejected (non-fast-forward)** when the workflow pushed state back,
   because the repo advanced between checkout and push (manual edits / overlap).
   Fixed: push step does rebase-and-retry; checkout uses `fetch-depth: 0`.
3. **Gemini "not working", zero requests to AI Studio.** Root cause: the API key
   was stored as an **environment secret** (`github-pages`), so only the deploy
   job could see it — the `run` job got nothing. Fixed by moving it to a
   **repository secret**.
4. **HTTP 404 from model ''.** An empty `GEMINI_MODEL` repo *variable* produced a
   blank model name. Fixed: code treats empty env var as unset (`or` fallback).
5. **503 model-overloaded errors during usage spikes.** Added a **model fallback
   chain** — on 503/429/5xx/404 it advances to the next model.
6. **Output truncated to ~4 words.** Cause: `maxOutputTokens: 80` on a *thinking*
   model — reasoning ate the budget. Fixed: raised to 800 and disabled thinking
   (`thinkingConfig.thinkingBudget = 0`). Added MAX_TOKENS detection/logging.
7. **Notes too short / not on-target.** Rewrote the prompt to an automotive
   regulatory-affairs analyst producing 2-3 sentences from the Description:
   what changed, relevance to the auto industry, and high/moderate/low impact.

## Known constraints & gotchas

- **Secrets are write-only** in GitHub — the edit screen shows them blank. That's
  normal, not a sign the secret is missing. Confirm via the run log
  (`GEMINI_API_KEY: ***` and the diagnostic `length:` line).
- **API key vs model:** key = repository **secret** `GEMINI_API_KEY`;
  model(s) = repository **variable** `GEMINI_MODELS` (comma-separated) or the
  single `GEMINI_MODEL`. An empty variable is now handled safely.
- **Model retirement:** `gemini-2.0-flash` was shut down June 1, 2026 (do not
  use). `gemini-2.5-flash` is slated to retire ~Oct 16, 2026 — refresh the
  default chain then. Current default chain:
  `gemini-2.5-flash` → `gemini-2.5-flash-lite` → `gemini-3-flash`.
- **Diagnostics:** the workflow has a non-fatal Gemini diagnostic step (raw curl
  + model list + self-test), uploads a `run-diagnostics` artifact (logs, report,
  state), and writes a run-summary panel. Use those before deep log-diving.
  `python monitor.py --force-llm-test` summarizes one entry regardless of new
  status, to prove the Gemini path end-to-end.

## RATE LIMITS — important for next session

- **Confirmed in AI Studio for this project: 5 RPM** (requests per minute) on the
  Gemini free tier. (Public docs sometimes say 10 for 2.5 Flash, but the live
  project quota is the source of truth, and it is 5.) Design to 5 RPM.
- The summarizer currently throttles to ~1 request / 6.5s ≈ 9/min — **this
  exceeds 5 RPM and must be lowered** (≥12s spacing) or replaced with a smarter
  scheduler (see open items).
- **Daily volume is not the problem.** Worst realistic case: 8 ePing countries,
  a few regulations/day → ~24 requests/day, far below daily caps. The bottleneck
  is purely per-minute pacing.

## Open items for next session

1. **UI improvements** to the report (`report_template.html` + possibly the data
   passed from `monitor.py`). Decide direction: grouping, filtering, severity
   badges, per-source sections, search, dark mode, etc.
2. **Add a second data source.** If it has a JSON API: a new fetcher + extractor
   pair and a `sites.json` entry. If JS-only: implement the `playwright_table`
   stub (uncomment Playwright in requirements + workflow).
3. **Rate-limit-safe scheduling for 5 RPM.** Candidate approaches to weigh:
   - **Hard throttle** to ≤5/min (simplest; one daily run, paced ~13s apart).
     ~24 notes ≈ 5-6 min run time — fine on Actions.
     This alone likely suffices given the low daily volume.
   - **Batch into one prompt:** send multiple notifications in a single request
     and get back a structured (e.g. JSON) array of notes — turns N requests
     into 1. Best token/qps efficiency; needs careful prompt + parsing.
   - **Multiple daily runs** (e.g. cron every few hours) each summarizing a slice
     of new items, if volume ever grows.
   - **Exponential backoff on 429** (partially present) plus the existing model
     fallback chain.
   Recommended starting point: hard-throttle to 5 RPM, and add single-request
   batching if/when volume grows or runs feel slow.

## Decisions locked in

- JSON state (not SQLite). Playwright opt-in per source. GitHub Pages public URL
  (data is public). Gemini optional + fails soft. First run = baseline.
  `track_limit` = 30 (tunable per source).
