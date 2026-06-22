# Notification Monitor

Checks a set of web sources on a schedule, detects **new entries** since the
last check, and publishes a clean HTML report. Built first for
[ePing](https://epingalert.org) (WTO TBT/SPS notifications), but designed to
add more sources easily.

- **No server needed** — runs free on GitHub Actions on a daily schedule.
- **State is a JSON file** committed back to the repo (full change history in Git).
- **Report is a single web page** served by GitHub Pages — non-technical users
  just bookmark one link.
- **Optional AI relevance note** per new item via Google Gemini (free tier).

---

## For non-technical viewers

Open the report link (your bookmark). Each box is a **new** notification since
the last check: a short title, a one-line relevance note, key details, and a
**"More information"** link to the official source. If there's nothing new, the
page says so. That's it — nothing to install.

---

## How it works (one run)

1. Reads `sites.json` (what to monitor) and `state/state.json` (what's been seen).
2. For each site: fetches data, reduces each item to a stable `uid`.
3. Anything whose `uid` isn't in the stored state is **new**.
4. Optionally asks Gemini for a one-sentence relevance note per new item.
5. Writes `docs/report.html` and updates `state/state.json`.

**First run for a source is a baseline** — it records what's currently there and
flags nothing as new. Real changes show from the second run onward.

---

## Run locally

```bash
pip install -r requirements.txt
python monitor.py
# open docs/report.html in a browser
```

Optional AI notes locally:

```bash
export GEMINI_API_KEY=your_key_here      # Windows: set GEMINI_API_KEY=...
python monitor.py
```

Disable AI notes even if a key is set: `python monitor.py --no-llm`.

---

## Deploy free on GitHub Actions + Pages

1. **Create a GitHub repo** and push these files.
2. **Enable Pages:** repo *Settings → Pages → Build and deployment →
   Source: GitHub Actions*.
3. **(Optional) Add the Gemini key:** *Settings → Secrets and variables →
   Actions → New repository secret*, name it `GEMINI_API_KEY`. Get a free key
   from Google AI Studio. To override the model, add a *Variable* named
   `GEMINI_MODEL` (default is a free-tier Flash model; Google occasionally
   renames these — see Notes).
4. The workflow (`.github/workflows/monitor.yml`) runs **daily at 06:00 UTC**
   and on demand (*Actions tab → Notification Monitor → Run workflow*). Change
   the `cron:` line to reschedule.
5. After the first run, your report lives at:
   `https://<your-username>.github.io/<repo-name>/`
   (this redirects to `report.html`). Bookmark and share that link.

The report URL is public-but-unguessable — fine for the public WTO data here.

---

## Add another source

### Same ePing API, different filter (easiest)

Add another entry to `sites.json` with different `params`
(`countryIds`, `freeText`, `domainIds`). Nothing else to write:

```json
{
  "id": "eping_food_safety",
  "name": "ePing — Food safety (SPS)",
  "description": "SPS notifications, free text 'food'.",
  "fetcher": "eping_api",
  "track_limit": 30,
  "params": { "domainIds": "2", "countryIds": ["C048"], "freeText": "food" }
}
```

### A different website

Two extension points, both in code:

- **`fetchers.py`** — write a function that returns a list of raw records.
  For a site with a JSON API, mirror `fetch_eping_api`. For a JS-rendered site
  with no API, use the `playwright_table` stub (uncomment Playwright in
  `requirements.txt` and the install step in the workflow).
- **`extractors.py`** — write a function turning those raw records into the
  uniform entry shape (`uid`, `title`, `summary`, `meta`, `link`, `extra_link`,
  `llm_input`). Register both under the same key in their respective registries
  and reference that key as `"fetcher"` in `sites.json`.

---

## Tuning

- **How many items per source:** `track_limit` in each site (currently 30).
- **Schedule:** `cron:` in the workflow.
- **State growth:** capped at `track_limit` per site — stays tiny.

---

## Notes / gotchas

- **First-run baseline** is intentional, so you don't get 30 "new" items on day one.
- **ePing `parentId`:** ePing groups related country notifications under a parent.
  Each item is tracked by its own `id`; related ones may appear separately. This
  is expected, not a bug.
- **Gemini free tier** is rate-limited (~10 requests/min, a few hundred/day on
  Flash) and model names change over time. The summarizer throttles and **fails
  soft** — if the key is missing, rate-limited, or a model is renamed, the report
  still generates without that note. Update `GEMINI_MODEL` if you see notes stop.
- **Sandbox vs. live:** development happened where `epingalert.org` was network-
  blocked, so the pipeline was validated against a saved real API response. On
  GitHub Actions (open internet) the live fetch works directly. The very first
  live run will baseline; trigger it once manually to confirm, then let the
  schedule take over.

## Files

```
monitor.py            engine (fetch → diff → summarize → render → save state)
sites.json            what to monitor
fetchers.py           how to fetch each source type
extractors.py         how to normalise each source type
summarizer.py         optional Gemini relevance note (fails soft)
report_template.html  the report's look
local_test.py         offline test against a saved real API response
state/state.json      seen-item memory (committed; do not delete)
docs/                 published report (GitHub Pages serves this)
.github/workflows/    the daily automation
```
