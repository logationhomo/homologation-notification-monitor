#!/usr/bin/env python3
"""
monitor.py — main engine.

Flow per run:
  1. Load config (sites.json) and prior state (state/state.json).
  2. For each site: fetch -> extract -> diff against stored state -> classify
     each entry as new/changed/unchanged.
  3. New or content-changed entries get a fresh Gemini note + severity.
     Unchanged entries CARRY FORWARD their stored note/severity/summarised_at.
  4. Render docs/report.html (and docs/report_data.json) for the FULL tracked
     set (not just new items) so the dashboard is never empty.
  5. Save updated enriched state (capped to track_limit, newest-first).

First run for a site = baseline: nothing is flagged "new", but the current set
IS summarised once so the dashboard is useful from day one.

STATE SCHEMA (C2 — "show the latest set; retain notes until overwritten"):
  state[site_id] = {
    "entries": [ {uid, title, summary, meta, link, extra_link,
                  note, severity, summarised_at, content_hash,
                  first_seen}, ... ],   # newest-first, capped at track_limit
    "last_checked": <iso8601>,
    "tracked_count": <int>,
  }
A content_hash captures the fields that define "did this notification change".
If a known uid comes back with a different hash, it is treated as NEW again
(re-summarised, re-flagged) so a stored note can never contradict live data.
"""

import os
import sys
import json
import hashlib
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

# Default severity when the LLM is disabled or produced nothing usable.
DEFAULT_SEVERITY = "unknown"

# Fields carried from an extracted entry into persistent state. (llm_input is
# deliberately NOT stored — it is regenerated from the live fetch each run and
# only needed at summarise time.)
_PERSIST_FIELDS = ("uid", "title", "summary", "meta", "link", "extra_link")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _today():
    """UTC date (YYYY-MM-DD) used to stamp when an entry was summarised."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _norm_list(value):
    """Normalise a comma-separated list field (e.g. document symbols, objectives)
    into an order- and whitespace-insensitive form, so that ePing merely
    REORDERING the list does not look like a content change. Splits on commas,
    strips each part, drops empties, sorts, and rejoins."""
    parts = [p.strip() for p in str(value or "").split(",")]
    parts = [p for p in parts if p]
    return ", ".join(sorted(parts))


def _content_hash(entry):
    """
    Stable hash of the fields that define whether a notification has materially
    changed. If any of these change for a known uid, we re-summarise and re-flag
    the item as new so the stored note can never describe a stale version.

    Covers the human-meaningful content (title, summary) and the meta fields a
    viewer/analyst would care about (deadline, products, objectives, symbols,
    member). The list-like fields (document symbols, objectives) are normalised
    order-insensitively so that a pure reordering by the upstream API does NOT
    trigger a false "changed" detection (and a wasted re-summarise).
    """
    meta = entry.get("meta", {}) or {}
    basis = {
        "title": (entry.get("title", "") or "").strip(),
        "summary": (entry.get("summary", "") or "").strip(),
        "meta": {
            "Notifying member": meta.get("Notifying member", ""),
            "Comment deadline": meta.get("Comment deadline", ""),
            "Distribution date": meta.get("Distribution date", ""),
            "Products": meta.get("Products", ""),
            "Objectives": _norm_list(meta.get("Objectives", "")),
            "Document symbol(s)": _norm_list(meta.get("Document symbol(s)", "")),
        },
    }
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _summarise_entry(entry, use_llm):
    """Run the summarizer for one entry and stamp note/severity/summarised_at.
    Centralised so every code path (new, changed, baseline) stays consistent."""
    if use_llm:
        result = summarizer.summarize(entry["llm_input"])
        entry["note"] = result.get("note", "")
        entry["severity"] = result.get("severity", DEFAULT_SEVERITY) or DEFAULT_SEVERITY
    else:
        entry["note"] = ""
        entry["severity"] = DEFAULT_SEVERITY
    # Stamp the analysis date only if we actually produced a note; a no-LLM run
    # leaves summarised_at empty so the UI doesn't show a misleading date.
    entry["summarised_at"] = _today() if entry.get("note") else ""


def process_site(site, prior_state, use_llm):
    """
    Returns (site_report, site_state).

    site_report carries the FULL tracked set (each entry tagged is_new), plus a
    new_count and last_change date for the dashboard banner. site_state is the
    enriched persistent state (entries, newest-first, capped at track_limit).
    """
    fetcher = fetchers.get_fetcher(site["fetcher"])
    extractor = extractors.get_extractor(site["fetcher"])
    track_limit = site.get("track_limit", 30)

    raw = fetcher(site)
    fetched = extractor(raw, site)
    fetched = fetched[:track_limit]

    prior = prior_state.get(site["id"])

    # Migration: the pre-C2 state stored only a `uids` list with no enriched
    # `entries`. If we see that shape, treat this run as a baseline (summarise
    # the current set once, flag nothing new) instead of flooding every item as
    # "new" just because the old format carried no content to diff against.
    prior_is_legacy = bool(prior) and "entries" not in prior and "uids" in prior
    is_baseline = prior is None or prior_is_legacy

    # Index prior entries by uid for carry-forward / change detection.
    prior_entries = {}
    if prior and not prior_is_legacy:
        for pe in prior.get("entries", []):
            uid = str(pe.get("uid", ""))
            if uid:
                prior_entries[uid] = pe

    today = _today()
    out_entries = []  # full tracked set, newest-first (fetch order is newest-first)

    for fe in fetched:
        uid = fe["uid"]
        chash = _content_hash(fe)
        prev = prior_entries.get(uid)

        # Decide: new, changed, or unchanged.
        if is_baseline:
            is_new = False          # baseline never flags new...
            needs_summary = True    # ...but DOES summarise once (decision (ii))
            first_seen = today
        elif prev is None:
            is_new = True           # unseen uid -> genuinely new
            needs_summary = True
            first_seen = today
        elif prev.get("content_hash") != chash:
            is_new = True           # known uid, content changed -> treat as new
            needs_summary = True
            first_seen = prev.get("first_seen", today)
        else:
            is_new = False          # known and unchanged -> carry forward
            needs_summary = False
            first_seen = prev.get("first_seen", today)

        entry = {k: fe.get(k) for k in _PERSIST_FIELDS}
        entry["content_hash"] = chash
        entry["first_seen"] = first_seen
        entry["is_new"] = is_new

        if needs_summary:
            entry["llm_input"] = fe["llm_input"]  # transient, for summarise only
            _summarise_entry(entry, use_llm)
            entry.pop("llm_input", None)
        else:
            # Carry forward the previously computed analysis verbatim.
            entry["note"] = prev.get("note", "")
            entry["severity"] = prev.get("severity", DEFAULT_SEVERITY) or DEFAULT_SEVERITY
            entry["summarised_at"] = prev.get("summarised_at", "")

        out_entries.append(entry)

    new_count = sum(1 for e in out_entries if e["is_new"])

    # last_change = most recent distribution date among NEW items this run; if
    # none are new, fall back to the newest distribution date in the set so the
    # banner can say "nothing new since <date>".
    def _dist(e):
        return (e.get("meta", {}) or {}).get("Distribution date", "") or ""

    new_dates = [_dist(e) for e in out_entries if e["is_new"] and _dist(e)]
    all_dates = [_dist(e) for e in out_entries if _dist(e)]
    if new_dates:
        last_change = max(new_dates)
    elif prior and prior.get("last_change"):
        last_change = prior.get("last_change")  # preserve across no-change runs
    elif all_dates:
        last_change = max(all_dates)
    else:
        last_change = ""

    # Persistent state: store the full enriched set (minus is_new, which is a
    # per-run view concern, not persistent truth).
    state_entries = []
    for e in out_entries:
        se = {k: e.get(k) for k in _PERSIST_FIELDS}
        se["note"] = e.get("note", "")
        se["severity"] = e.get("severity", DEFAULT_SEVERITY)
        se["summarised_at"] = e.get("summarised_at", "")
        se["content_hash"] = e.get("content_hash", "")
        se["first_seen"] = e.get("first_seen", today)
        state_entries.append(se)

    site_report = {
        "id": site["id"],
        "name": site["name"],
        "description": site.get("description", ""),
        "is_baseline": is_baseline,
        "tracked_count": len(out_entries),
        "new_count": new_count,
        "last_change": last_change,
        "entries": out_entries,          # FULL set, each tagged is_new
        "new_entries": [e for e in out_entries if e["is_new"]],  # kept for compat
    }
    site_state = {
        "entries": state_entries,
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_change": last_change,
        "tracked_count": len(out_entries),
    }
    return site_report, site_state


# Fields exposed to the report JSON per entry (the UI's adapter reads these).
_REPORT_ENTRY_FIELDS = ("uid", "title", "summary", "meta", "link", "extra_link",
                        "note", "severity", "summarised_at", "first_seen", "is_new")


def render_report(site_reports, report_path):
    env = Environment(
        loader=FileSystemLoader(HERE),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report_template.html")
    total_new = sum(s.get("new_count", len(s.get("new_entries", []))) for s in site_reports)
    total_tracked = sum(s.get("tracked_count", 0) for s in site_reports)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = template.render(
        sites=site_reports, total_new=total_new, generated_at=generated_at
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    data_path = os.path.join(os.path.dirname(report_path), "report_data.json")
    save_json(data_path, {
        "generated_at": generated_at,
        "total_new": total_new,
        "total_tracked": total_tracked,
        "sites": [
            {
                "id": s["id"], "name": s["name"],
                "is_baseline": s.get("is_baseline", False),
                "tracked_count": s.get("tracked_count", 0),
                "new_count": s.get("new_count", len(s.get("new_entries", []))),
                "last_change": s.get("last_change", ""),
                # Full tracked set so the dashboard is never empty.
                "entries": [
                    {k: e.get(k) for k in _REPORT_ENTRY_FIELDS}
                    for e in s.get("entries", [])
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
    ap.add_argument("--force-llm-test", action="store_true",
                    help="Summarize the first fetched entry per site regardless "
                         "of whether it is new — proves the Gemini path works.")
    args = ap.parse_args()

    config = load_json(args.sites, {"sites": []})
    prior_state = load_json(args.state, {})
    use_llm = summarizer.is_enabled() and not args.no_llm

    print(f"Loaded {len(config['sites'])} site(s). "
          f"LLM notes: {'ON' if use_llm else 'OFF'}")
    if use_llm:
        print(f"  Using Gemini model: {summarizer.DEFAULT_MODEL} "
              f"(override with GEMINI_MODEL)")

    if args.force_llm_test:
        if not summarizer.is_enabled():
            print("force-llm-test: GEMINI_API_KEY not set; nothing to test.")
            sys.exit(1)
        import fetchers as _f, extractors as _e
        for site in config["sites"]:
            try:
                raw = _f.get_fetcher(site["fetcher"])(site)
                entries = _e.get_extractor(site["fetcher"])(raw, site)
                if not entries:
                    print(f"  [{site['id']}] no entries fetched to test.")
                    continue
                result = summarizer.summarize(entries[0]["llm_input"])
                if result.get("note"):
                    status = (f"severity={result.get('severity')!r} "
                              f"note={result.get('note')[:80]!r}")
                else:
                    status = "EMPTY (see [summarizer] log above)"
                print(f"  [{site['id']}] forced summary -> {status}")
            except Exception as ex:
                print(f"  [{site['id']}] force-llm-test error: {ex}", file=sys.stderr)
        sys.exit(0)

    site_reports = []
    new_state = dict(prior_state)
    had_error = False

    for site in config["sites"]:
        try:
            report, state = process_site(site, prior_state, use_llm)
            site_reports.append(report)
            new_state[site["id"]] = state
            if report["is_baseline"]:
                tag = f"baseline ({report['tracked_count']} summarised)"
            else:
                tag = f"{report['new_count']} new"
            print(f"  [{site['id']}] OK — {report['tracked_count']} tracked, {tag}")
        except Exception as e:
            had_error = True
            print(f"  [{site['id']}] ERROR: {e}", file=sys.stderr)
            # On fetch failure, preserve prior state's entries so the dashboard
            # keeps showing the last-known set instead of going blank.
            prior = prior_state.get(site["id"]) or {}
            carried = prior.get("entries", [])
            for ce in carried:
                ce = dict(ce)
                ce["is_new"] = False
                # leave note/severity/summarised_at as stored
            site_reports.append({
                "id": site["id"], "name": site["name"],
                "description": site.get("description", "") + "  (fetch failed this run)",
                "is_baseline": False,
                "tracked_count": len(carried),
                "new_count": 0,
                "last_change": prior.get("last_change", ""),
                "entries": [dict(ce, is_new=False) for ce in carried],
                "new_entries": [],
            })

    total_new = render_report(site_reports, args.report)
    save_json(args.state, new_state)

    print(f"Done. {total_new} new item(s). "
          f"Report: {args.report} | State: {args.state}")
    sys.exit(1 if had_error else 0)


if __name__ == "__main__":
    main()
