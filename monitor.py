#!/usr/bin/env python3
"""
monitor.py — main engine.

Run:  python monitor.py
Flow per run:
  1. Load config (sites.json) and prior state (state/state.json).
  2. For each site: fetch -> extract -> diff against stored uids -> new entries.
  3. (Optional) Gemini relevance note per new entry.
  4. Render docs/report.html (and docs/report_data.json).
  5. Save updated state (capped to track_limit, newest-first).

First run for a site = baseline: record current uids, flag nothing as "new".

Environment:
  GEMINI_API_KEY   optional; enables relevance notes
  GEMINI_MODEL     optional; defaults to a free-tier Flash model
  STATE_PATH       optional; override state file location
  REPORT_PATH      optional; override report output location
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

import fetchers
import extractors
import summarizer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SITES = os.path.join(HERE, "sites.json")
DEFAULT_STATE = os.environ.get("STATE_PATH", os.path.join(HERE, "state", "state.json"))
DEFAULT_REPORT = os.environ.get("REPORT_PATH", os.path.join(HERE, "docs", "report.html"))


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def process_site(site, prior_state, use_llm):
    """
    Returns a dict describing this site's results for the report, plus the
    updated per-site state to persist.
    """
    fetcher = fetchers.get_fetcher(site["fetcher"])
    extractor = extractors.get_extractor(site["fetcher"])
    track_limit = site.get("track_limit", 30)

    raw = fetcher(site)
    entries = extractor(raw, site)            # uniform shape, newest-first
    entries = entries[:track_limit]

    current_uids = [e["uid"] for e in entries]
    prior = prior_state.get(site["id"])
    is_baseline = prior is None
    seen_uids = set(prior.get("uids", [])) if prior else set()

    if is_baseline:
        new_entries = []                      # don't flag everything on first run
    else:
        new_entries = [e for e in entries if e["uid"] not in seen_uids]

    # Optional LLM relevance note (only for genuinely new entries).
    if use_llm and new_entries:
        for e in new_entries:
            e["llm_note"] = summarizer.summarize(e["llm_input"])
    else:
        for e in new_entries:
            e["llm_note"] = ""

    # New state: union of current uids and what we'd seen, capped & newest-first.
    # Current uids are already newest-first from the API; keep their order,
    # then top up with previously-seen ones not in the current page.
    merged = current_uids + [u for u in prior.get("uids", []) if u not in set(current_uids)] if prior else current_uids
    merged = merged[:max(track_limit, len(current_uids))]

    site_report = {
        "id": site["id"],
        "name": site["name"],
        "description": site.get("description", ""),
        "is_baseline": is_baseline,
        "tracked_count": len(current_uids),
        "new_entries": new_entries,
    }
    site_state = {
        "uids": merged,
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tracked_count": len(current_uids),
    }
    return site_report, site_state


def render_report(site_reports, report_path):
    env = Environment(
        loader=FileSystemLoader(HERE),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report_template.html")
    total_new = sum(len(s["new_entries"]) for s in site_reports)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = template.render(
        sites=site_reports, total_new=total_new, generated_at=generated_at
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Machine-readable companion file (handy for other tools / debugging).
    data_path = os.path.join(os.path.dirname(report_path), "report_data.json")
    save_json(data_path, {
        "generated_at": generated_at,
        "total_new": total_new,
        "sites": [
            {
                "id": s["id"], "name": s["name"], "is_baseline": s["is_baseline"],
                "tracked_count": s["tracked_count"],
                "new_entries": [
                    {k: e[k] for k in ("uid", "title", "summary", "meta",
                                       "link", "extra_link", "llm_note")}
                    for e in s["new_entries"]
                ],
            } for s in site_reports
        ],
    })
    return total_new


def main():
    ap = argparse.ArgumentParser(description="Notification monitor")
    ap.add_argument("--sites", default=DEFAULT_SITES)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--no-llm", action="store_true",
                    help="Disable Gemini notes even if GEMINI_API_KEY is set")
    args = ap.parse_args()

    config = load_json(args.sites, {"sites": []})
    prior_state = load_json(args.state, {})
    use_llm = summarizer.is_enabled() and not args.no_llm

    print(f"Loaded {len(config['sites'])} site(s). "
          f"LLM notes: {'ON' if use_llm else 'OFF'}")

    site_reports = []
    new_state = dict(prior_state)
    had_error = False

    for site in config["sites"]:
        try:
            report, state = process_site(site, prior_state, use_llm)
            site_reports.append(report)
            new_state[site["id"]] = state
            tag = ("baseline" if report["is_baseline"]
                   else f"{len(report['new_entries'])} new")
            print(f"  [{site['id']}] OK — {report['tracked_count']} tracked, {tag}")
        except Exception as e:
            had_error = True
            print(f"  [{site['id']}] ERROR: {e}", file=sys.stderr)
            # Keep prior state for this site; show an error card in the report.
            site_reports.append({
                "id": site["id"], "name": site["name"],
                "description": site.get("description", "") + "  (fetch failed this run)",
                "is_baseline": False, "tracked_count": 0, "new_entries": [],
            })

    total_new = render_report(site_reports, args.report)
    save_json(args.state, new_state)

    print(f"Done. {total_new} new item(s). "
          f"Report: {args.report} | State: {args.state}")
    # Non-zero exit on hard errors so CI surfaces them, but report is still written.
    sys.exit(1 if had_error else 0)


if __name__ == "__main__":
    main()
