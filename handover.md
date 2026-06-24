# Handover — Notification Monitor

_Last updated: 2026-06-24. Read this first when resuming._

## How to resume in a new session (token-efficient)

A new chat has no memory of this one. To pick up efficiently:

1. **Upload the project** at the start of the next session (repo zip or latest
   project zip). Claude reads files directly from the upload; no pasting code.
2. **Upload this `handover.md`** alongside it — it carries all decisions so
   nothing is re-derived.
3. **Upload `eping_API_response_1.json`** (the saved real API sample) — it's how
   the pipeline is validated offline, since the live ePing endpoint is often
   network-blocked in the dev sandbox.
4. **For coding-heavy work**, prefer a setup that reads the GitHub repo directly
   (Claude Code or a repo connection) so edits map straight back.
5. Keep `handover.md` committed in the repo so it stays versioned with the code.

---

## What this project is

An automated monitor that checks web data sources daily, detects **new entries**
since the last check, and publishes a clean HTML report. First (and currently
only) source: **ePing** (WTO TBT/SPS trade notifications), filtered for GCC +
Morocco vehicle regulations (ICS code 43.020).

**Product framing (decided this session):** this is a **notification + regulatory
intelligence** tool, NOT a regulatory *archive*. It optimizes for "what changed,
why it matters, what to do" — recency and signal, not completeness/storage. This
justifies: small `track_limit`, tiny JSON state (no database), and history being
a minor convenience rather than a core feature. Building a full archive would be
a deliberate pivot (add a datastore), not something to half-build now.

**Audience:** non-technical viewers who open one bookmarked link and read what's
new, each with a link to the official source, an AI relevance note, and a
severity rating.

## Architecture (and why)

- **Runner:** GitHub Actions, daily cron + manual trigger. Free, no server.
- **State:** committed JSON (`state/state.json`) — Git gives history; chosen over
  SQLite because state is tiny and Git-diffable.
- **Fetching:** ePing JSON API (`/api/v1/azureSearch/getAll`), no browser needed.
  Playwright stubbed for future JS-only sources.
- **Summaries + severity:** Google Gemini (free tier), optional, fails soft.
- **Delivery:** GitHub Pages serves `docs/report.html` at a fixed URL.
- **Report rendering (NEW this session):** `report.html` is now a thin shell that
  **fetches `docs/report_data.json`** and renders client-side JS (master-detail
  UI). The HTML stays tiny/cacheable; the data is a separate cacheable file. This
  scales cleanly and keeps one source of truth (the JSON the pipeline already
  emitted).

## Current status: WORKING (pipeline) + NEW UI ported, pending live-API confirm

- End-to-end pipeline confirmed on GitHub Actions previously: fetch → detect new
  → Gemini note → report → commit state → Pages.
- **This session added structured severity + a new master-detail report UI.**
  Both validated offline against the real API sample and via browser tests, but
  the structured-severity LLM output and the new report have **not yet been seen
  on a live Actions run**. First live run is the true confirmation (see Open items).

## Repo file map

```
monitor.py            engine: fetch -> diff -> summarize(note+severity) -> render -> save state
sites.json            what to monitor (ePing GCC/43.020 configured)
fetchers.py           per-source fetchers: eping_api (live), playwright_table (stub)
extractors.py         normalises raw records to a uniform entry shape
summarizer.py         Gemini relevance note + STRUCTURED SEVERITY; model fallback; fails soft
report_template.html  NEW: thin shell that fetches report_data.json + master-detail UI JS
local_test.py         offline test against a saved real ePing API response
state/state.json      seen-item memory (committed; do NOT delete)
docs/                 published report (GitHub Pages serves this)
  report.html         rendered shell (Jinja passes the JS through untouched)
  report_data.json    the data the shell fetches (now includes per-entry severity)
  index.html          redirect to report.html (unchanged)
.github/workflows/monitor.yml   daily automation + diagnostics + Pages deploy
README.md             setup + how to add sources + how non-technical users read it
handover.md           this file
```

---

## What changed THIS session (2026-06-24)

### 1. Structured severity (pipeline)
- **`summarizer.py`** is the substantive change:
  - `summarize()` now returns a **dict** `{"note": str, "severity": str}` instead
    of a bare string. **Contract change** — all callers updated.
  - Severity is one of `high` / `moderate` / `low` / `unknown`.
  - New JSON system prompt with an explicit high/moderate/low rubric; added
    `responseMimeType: "application/json"` to generationConfig (verified a real,
    current Gemini API field). Tolerant parser (`_parse_structured`) handles code
    fences, preamble, trailing text, bad/missing severity (salvages note, coerces
    sev to `unknown`), and garbage (neutral default).
  - **Throttle raised from 6.5s to 13s** (`_MIN_INTERVAL_S`) to respect the
    confirmed **5 RPM** project quota. (The old 6.5s ≈ 9/min exceeded it — this
    was flagged as a bug in the previous handover; now fixed.)
- **Fallback default is `unknown`, deliberately NOT `low`.** Defaulting a no-LLM
  or unparseable run to "low" would silently mislabel everything as low-risk in a
  risk tool. "unknown" is honest; the UI renders it neutral grey.
- **`monitor.py`**: `process_site` captures note+severity via a centralized
  `_apply_summary` helper; `--no-llm`/no-key paths default severity to `unknown`;
  `--force-llm-test` updated to the dict contract; `report_data.json` now includes
  a `severity` field per entry.
- **`extractors.py`**: effectively unchanged (severity is added in monitor.py, not
  the extractor). The recreated copy matches the prior version.

### 2. New report UI — master-detail (v3.2)
- The report went from a simple stacked-cards page to a **master-detail
  dashboard**: left list pane (grouped source → country, severity-first compact
  rows) + right detail pane.
- **Interaction model (decided after iteration):** empty base state (NO
  auto-select), full-width list; clicking a row opens the detail pane and the list
  shrinks beside it; ✕ Close or Esc returns to the full-width base.
  - Why no auto-select: with multiple sources it forces an arbitrary "which
    source's item is most important" judgment the tool shouldn't make.
- **Features:** live search; severity filter (high/mod/low; unknown shows under
  "All"); country filter; deadline urgency badges (computed "due in N days",
  red ≤7 / amber ≤21 / grey beyond / struck-through if closed); `#uid=` deep
  links + "Copy link" (shareable, still one static file); dark mode; thin
  theme-aware scrollbars; row-stays-put-on-open anchoring.
- **Schema adapter** in the template maps the real pipeline shape (entries with a
  `meta` dict; `report_data.json` carries only NEW entries) to the flat shape the
  UI JS uses. This is the key integration seam — if `report_data.json`'s shape
  changes, update `adaptEntry()` in `report_template.html`.
- **History:** `report_data.json` only contains new items by design, so the UI
  shows an honest one-line note ("Older items are not retained here") instead of
  fake history rows. Consistent with the intelligence-not-archive framing.

### Mockup lineage (reference only — superseded)
During design we produced a chain of standalone mockups with DUMMY data:
`direction_a_dashboard` (v1) → `direction_b_briefing` (alt, not chosen) →
`direction_a_v2` (triage row) → `direction_a_v3_masterdetail` →
`direction_a_v3_1_masterdetail` (interaction fixes) →
`direction_a_v3_2_masterdetail` (scroll polish). **Only v3.2's design shipped**,
into `report_template.html`. The mockups are kept as reference but are NOT wired
to the pipeline — do not mistake them for the live UI or iterate on them; iterate
on `report_template.html`.

---

## How detection works (unchanged)

Each ePing notification has a stable `id`. State stores seen `id`s per site.
New = any `id` not previously seen. **First run for a source is a baseline**
(records current ids, flags nothing) so you don't get a day-one flood.
`track_limit` (currently 30) caps how many recent items are tracked per source.

---

## Debugging arc / decisions THIS session

1. **UI critique pass.** Ran a fresh-eyes critique (rendered screenshots via
   headless Chromium). Key fixes that shaped v2+: severity must dominate (was
   out-shouted by the green "New" badge); triage should not require a click (added
   a one-line relevance preview to collapsed rows); surface deadline urgency.
2. **Master-detail vs inline-expand.** Chose master-detail (v3) for scroll
   stability on long lists + shareable per-item URLs. Then refined interaction to
   empty-base/click-open/close (v3.1).
3. **Scroll polish (v3.2).** Two-scroll model kept but made deliberate: thin
   theme-aware scrollbars, `overscroll-behavior: contain`, detail resets to top on
   new selection, removed a 4px page-scroll leak (flex layout instead of
   `calc(100vh - 53px)`), and fixed a list "jump on open". The jump's real cause
   was the **column-width transition re-wrapping titles** (not the preview hiding),
   so the fix re-anchors the clicked row on `transitionend`. (This took several
   iterations — noted so it isn't re-litigated.)
4. **Option A vs B for the real report.** Chose A (embed/fetch data + client
   renders) over B (Jinja renders static rows). At realistic N (≤~300 entries)
   both are fine for render; A reuses the tested v3.2 JS verbatim and keeps one
   data source of truth. **Critical note:** render architecture is NOT the scale
   bottleneck — the **summarizer at 5 RPM** is. 300 new items ≈ ~65 min of
   summarization. Optimize throughput there (batching), not rendering.

---

## Known constraints & gotchas

- **Live Gemini not reachable in the dev sandbox**, so structured-severity was
  validated with MOCKED HTTP responses (realistic Gemini envelopes) + the offline
  sample. Plumbing is fully tested; live severities need a real Actions run.
- **`responseMimeType: application/json`** is set. Optional future hardening: add
  a `responseSchema` with a severity enum to force the shape — NOT done, because a
  complex schema can itself 400 and the tolerant parser already covers realistic
  failures. Good balance for free-tier Flash.
- **Secrets are write-only** in GitHub (edit screen shows blank — normal).
  Confirm via run log (`GEMINI_API_KEY: ***` + diagnostic `length:` line).
- **API key vs model:** key = repository **secret** `GEMINI_API_KEY`; model(s) =
  repository **variable** `GEMINI_MODELS` (comma-sep) or single `GEMINI_MODEL`.
  Empty variable handled safely.
- **Model retirement:** `gemini-2.0-flash` shut down 2026-06-01 (do not use).
  `gemini-2.5-flash` slated to retire ~2026-10-16 — refresh the default chain
  then. Current chain: `gemini-2.5-flash` → `gemini-2.5-flash-lite` →
  `gemini-3-flash`.
- **report_template.html is mostly JS, not Jinja.** It currently uses NO `{{ }}`
  Jinja tags (data comes via fetch). Jinja still renders it (autoescape on) and
  passes the JS through untouched. If you reintroduce Jinja variables, watch for
  `{{`/`{%` collisions with any JS.
- **GitHub Actions scheduled run** missed once at 06:00 UTC (no disable banner →
  most likely a skipped/delayed scheduled run, which is normal best-effort cron).
  Not yet hardened. See open items.

---

## RATE LIMITS — still important

- **Confirmed 5 RPM** on the Gemini free tier for this project (AI Studio).
  Design to 5 RPM. The summarizer now throttles to **13s spacing (~4-5/min)** —
  compliant. (Previously 6.5s, which violated it.)
- **Daily volume is not the problem.** Worst realistic case ~24 requests/day. The
  bottleneck is per-minute pacing → matters only when many new items land at once.

---

## Open items for next session (priority order)

1. **First LIVE Actions run** to confirm: (a) structured severity comes back from
   real Gemini and renders, (b) the new fetch-based report loads on Pages (verify
   `report_data.json` is served alongside `report.html` — it is committed by the
   workflow's `git add docs/...`; double-check the path). Use `python summarizer.py`
   (selftest) and `python monitor.py --force-llm-test` to prove the Gemini path.
2. **Harden the schedule** (the missed-run issue): move cron off the top of the
   hour (e.g. `7 6 * * *`) and consider always-committing state so the 60-day
   inactivity auto-disable can never trigger.
3. **Summarizer batching for 5 RPM** (the real scale work): send multiple
   notifications in one request, get back a JSON array of {note, severity} — turns
   N requests into 1. Best throughput; needs careful prompt + parsing. Hard
   throttle to ≤5/min is the simpler interim (already in place via 13s spacing).
4. **Add a second data source** (multi-source): a new fetcher+extractor pair + a
   sites.json entry (JSON-API source), or implement the `playwright_table` stub
   (JS-only source). The UI already groups by source and will show multiple
   sources automatically. Revisit UI grouping/filtering once >1 source exists.
5. **Mobile polish.** The responsive collapse (below 720px: list → full-screen
   detail with back) is FUNCTIONAL but not designed. Web-first was the call; do a
   real mobile pass when ready.
6. **Richer history** — only if the product ever shifts toward archive. Per the
   intelligence-not-archive framing, currently intentionally minimal. Would need a
   state-schema change to store more than uids.

---

## Decisions locked in

- Product = notification/intelligence, NOT archive.
- UI = master-detail (v3.2), empty base, click-to-open, ✕/Esc close. No auto-select.
- Severity = structured LLM field; fallback `unknown` (not `low`); UI grey for unknown.
- Report = Option A (thin shell fetches report_data.json + client JS).
- summarize() returns {note, severity}. 13s throttle for 5 RPM.
- JSON state (not SQLite). Playwright opt-in per source. Pages public URL
  (data is public). Gemini optional + fails soft. First run = baseline.
  `track_limit` = 30 (tunable per source).
