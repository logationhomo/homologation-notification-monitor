"""
summarizer.py — OPTIONAL Gemini relevance note for each new entry.

Design principles:
  - Entirely optional. If GEMINI_API_KEY is not set, every call returns ""
    and the rest of the pipeline behaves as if summaries were never requested.
  - Fails soft. Network errors, rate limits (HTTP 429), bad responses -> ""
    for that one entry. One bad call never crashes the run or aborts others.
  - Throttled. Free-tier Gemini Flash is ~10 requests/minute, so we space
    calls out and back off on 429s.

Model + endpoint are read from env so they can be updated without code changes
(Google rotates free-tier model names; see README).
"""

import os
import time
import json
import urllib.request
import urllib.error

# Defaults chosen for the free tier (Flash family). Override via env if Google
# renames models. As of mid-2026 the free tier is Flash / Flash-Lite only.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Conservative spacing for ~10 RPM free tier: ~1 request / 6.5s.
_MIN_INTERVAL_S = 6.5
_last_call_ts = [0.0]  # mutable holder so the throttle persists across calls

SYSTEM_PROMPT = (
    "You are a trade-compliance analyst. Given one WTO TBT/SPS notification, "
    "write ONE concise sentence (max 30 words) stating what it concerns and "
    "who/what it affects. No preamble, no markdown, just the sentence."
)


def is_enabled():
    return bool(os.environ.get("GEMINI_API_KEY"))


def summarize(entry_text, _key=None, _model=None):
    """
    Return a one-line relevance note for the given text, or "" if disabled
    or anything goes wrong. Never raises.
    """
    api_key = _key if _key is not None else os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return ""

    model = _model or DEFAULT_MODEL
    url = f"{GEMINI_BASE}/{model}:generateContent"

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": entry_text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 80},
    }
    data = json.dumps(body).encode("utf-8")

    # Throttle to respect free-tier RPM.
    elapsed = time.time() - _last_call_ts[0]
    if elapsed < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - elapsed)

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )

    for attempt in range(2):  # one retry on transient/rate-limit errors
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            _last_call_ts[0] = time.time()
            return _extract_text(payload)
        except urllib.error.HTTPError as e:
            _last_call_ts[0] = time.time()
            if e.code == 429 and attempt == 0:
                time.sleep(15)  # rate limited: brief backoff then one retry
                continue
            return ""  # any other HTTP error -> skip summary for this entry
        except Exception:
            _last_call_ts[0] = time.time()
            return ""
    return ""


def _extract_text(payload):
    """Pull the first text part out of a Gemini generateContent response."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts).strip()
        return " ".join(text.split())  # collapse whitespace
    except (KeyError, IndexError, TypeError):
        return ""
