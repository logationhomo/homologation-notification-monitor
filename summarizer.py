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
import sys
import time
import json
import urllib.request
import urllib.error

# Model fallback chain. We try these in order; on a retryable/overload error
# (503, 429, 5xx) or a model-not-found (404), we advance to the next one.
#
# Configure via env:
#   GEMINI_MODELS  comma-separated list, highest priority first  (preferred)
#   GEMINI_MODEL   single model; used as the first entry if GEMINI_MODELS unset
#
# Defaults are free-tier-capable, non-deprecated models (verified mid-2026).
# Order: a solid default, then a higher-RPD-limit lite model, then a
# next-generation Flash as a forward-looking backstop.
# IMPORTANT: gemini-2.0-flash / 2.0-flash-lite were SHUT DOWN June 1, 2026 —
# do not add them back. gemini-2.5-flash is slated to retire Oct 16, 2026, so
# revisit this list around then (or just set GEMINI_MODELS to override).
_DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-flash"]


def _model_chain():
    """Resolve the ordered list of models to try, from env or defaults."""
    raw = os.environ.get("GEMINI_MODELS") or ""
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if models:
        return models
    single = os.environ.get("GEMINI_MODEL") or ""
    single = single.strip()
    if single:
        # Keep the user's choice first, then the rest of the defaults as backups.
        return [single] + [m for m in _DEFAULT_MODELS if m != single]
    return list(_DEFAULT_MODELS)


# First model in the chain — used for the startup log line and self-test.
DEFAULT_MODEL = _model_chain()[0]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# HTTP codes where trying a DIFFERENT model may help (overload/transient/missing).
_RETRYABLE_CODES = {429, 500, 502, 503, 504, 404}

# Conservative spacing for ~10 RPM free tier: ~1 request / 6.5s.
_MIN_INTERVAL_S = 6.5
_last_call_ts = [0.0]  # mutable holder so the throttle persists across calls

SYSTEM_PROMPT = (
    "You are an automotive regulatory-affairs analyst. You will be given one "
    "WTO TBT/SPS notification (title, notifying member, products, objectives, "
    "and a Description of the regulatory change).\n\n"
    "Write a short relevance note of 2-3 sentences, based primarily on the "
    "Description, covering:\n"
    "1. What the regulatory change actually requires or changes.\n"
    "2. How it is relevant to the automobile industry (e.g. which vehicles, "
    "components, or processes it affects).\n"
    "3. How significant the change is for manufacturers/importers — flag it as "
    "high, moderate, or low impact and say why in a few words.\n\n"
    "Be concrete and specific. Plain text only: no markdown, no headings, no "
    "bullet points, no preamble — just the note itself."
)


def is_enabled():
    return bool(os.environ.get("GEMINI_API_KEY"))


def summarize(entry_text, _key=None, _model=None):
    """
    Return a one-line relevance note for the given text, or "" if disabled
    or all models fail. Never raises.

    Walks the model fallback chain: on a retryable error (503 overload, 429
    rate limit, other 5xx, or 404 model-not-found) it advances to the next
    model. Stops and returns on the first success, or on a non-retryable error
    (e.g. 400 bad request, 403 auth) where switching models would not help.
    """
    api_key = _key if _key is not None else os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return ""

    # If a specific model is forced (tests), try only that one; otherwise chain.
    models = [_model] if _model else _model_chain()

    last_was_retryable = False
    for i, model in enumerate(models):
        text, retryable = _call_once(entry_text, api_key, model)
        if text:
            if i > 0:
                _log(f"succeeded with fallback model '{model}' "
                     f"(after {i} model(s) failed)")
            return text
        if not retryable:
            # A different model won't fix this (bad key, malformed request, etc.)
            return ""
        last_was_retryable = True
        if i + 1 < len(models):
            _log(f"model '{model}' unavailable; trying fallback "
                 f"'{models[i + 1]}'")
    if last_was_retryable:
        _log("all models in the fallback chain were unavailable; "
             "skipping summary for this entry")
    return ""


def _call_once(entry_text, api_key, model):
    """
    One attempt against one model. Includes a single in-model retry for 429
    (rate limit) since that often clears after a short wait.

    Returns (text, retryable):
      text       the summary string, or "" on failure
      retryable  True if a DIFFERENT model might succeed (so the caller should
                 advance the chain); False for terminal errors.
    """
    url = f"{GEMINI_BASE}/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": entry_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            # Generous budget: Gemini 2.5/3 are "thinking" models that spend
            # output tokens on internal reasoning BEFORE the visible answer.
            # A small cap (e.g. 80) gets consumed by reasoning and truncates the
            # reply mid-sentence. 800 leaves ample room for a 2-3 sentence note.
            "maxOutputTokens": 800,
            # Disable thinking for this simple summarisation task: faster,
            # cheaper, and no tokens wasted on reasoning. Recognised by Gemini
            # 2.5 Flash / Flash-Lite; ignored harmlessly by models that don't
            # support it (the larger token budget covers those anyway).
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    data = json.dumps(body).encode("utf-8")

    for attempt in range(2):  # one in-model retry, used only for 429
        # Throttle to respect free-tier RPM (shared across all calls).
        elapsed = time.time() - _last_call_ts[0]
        if elapsed < _MIN_INTERVAL_S:
            time.sleep(_MIN_INTERVAL_S - elapsed)

        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            _last_call_ts[0] = time.time()
            text = _extract_text(payload)
            if not text:
                _log(f"empty response from model '{model}': "
                     f"{json.dumps(payload)[:300]}")
                # 200 but no text (e.g. safety block) — a different model might
                # behave differently, so treat as retryable.
                return "", True
            return text, False
        except urllib.error.HTTPError as e:
            _last_call_ts[0] = time.time()
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            _log(f"HTTP {e.code} from model '{model}': {detail}")
            if e.code == 429 and attempt == 0:
                time.sleep(15)   # brief backoff, then one same-model retry
                continue
            return "", (e.code in _RETRYABLE_CODES)
        except Exception as e:
            _last_call_ts[0] = time.time()
            _log(f"request failed for model '{model}': {type(e).__name__}: {e}")
            # Network blips etc. — another model on the same network likely
            # won't help, but it's cheap to let the chain try once. Treat as
            # retryable so a transient issue doesn't kill the summary outright.
            return "", True
    return "", True


def _log(msg):
    """Diagnostics go to stderr so they appear in GitHub Actions logs but never
    pollute the report. Silent only when there's genuinely nothing to say."""
    print(f"[summarizer] {msg}", file=sys.stderr)


def selftest():
    """
    One-shot connectivity/credential check. Returns (ok, message).
    Run via:  python summarizer.py
    Useful as a workflow step to see exactly what's wrong with Gemini.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return False, "GEMINI_API_KEY is not set in the environment."
    model = DEFAULT_MODEL
    out = summarize("Say the single word: OK", _key=key, _model=model)
    if out:
        return True, f"Gemini reachable via model '{model}'. Sample reply: {out!r}"
    return False, (
        f"Gemini call to model '{model}' returned no text. "
        f"See the [summarizer] log line above for the HTTP code / reason. "
        f"Common causes: wrong/renamed model (404), quota or region (429/403), "
        f"or an invalid key (400/403)."
    )


def _extract_text(payload):
    """Pull the first text part out of a Gemini generateContent response.
    Also surfaces a truncation warning if the model hit the output cap."""
    try:
        cand = payload["candidates"][0]
        finish = cand.get("finishReason", "")
        if finish == "MAX_TOKENS":
            # The note was cut off by the token budget. With thinking disabled
            # and an 800-token cap this should not happen for a 2-3 sentence
            # note, but log it so it's obvious if a future change reintroduces it.
            _log("response hit MAX_TOKENS — output truncated; consider raising "
                 "maxOutputTokens or shortening the prompt.")
        parts = cand["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts).strip()
        return " ".join(text.split())  # collapse whitespace
    except (KeyError, IndexError, TypeError):
        return ""


def list_models():
    """
    Ask the API which models THIS key can actually use, and which support
    generateContent. This is the definitive answer to 'which model should I set'.
    Run via:  python summarizer.py --list
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("[summarizer] GEMINI_API_KEY is not set.", file=sys.stderr)
        return
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    req = urllib.request.Request(url, headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:400]
        except Exception:
            pass
        print(f"[summarizer] models list failed: HTTP {e.code}: {body}",
              file=sys.stderr)
        return
    except Exception as e:
        print(f"[summarizer] models list failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return

    print("Models available to your key that support generateContent:")
    for m in data.get("models", []):
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            name = m.get("name", "").replace("models/", "")
            print(f"  - {name}")


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_models()
    else:
        ok, msg = selftest()
        print(("OK: " if ok else "FAILED: ") + msg)
        sys.exit(0 if ok else 1)
