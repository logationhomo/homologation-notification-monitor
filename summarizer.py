"""
summarizer.py — OPTIONAL Gemini relevance note + structured severity per entry.

Design principles (unchanged):
  - Entirely optional. If GEMINI_API_KEY is not set, every call returns the
    neutral default ({"note": "", "severity": "unknown"}) and the rest of the
    pipeline behaves as if summaries were never requested.
  - Fails soft. Network errors, rate limits (HTTP 429), bad/[]malformed
    responses -> the neutral default for that one entry. One bad call never
    crashes the run or aborts others.
  - Throttled. Free-tier Gemini Flash is rate-limited, so we space calls out
    and back off on 429s.

CONTRACT CHANGE (vs the string-returning version):
  summarize() now returns a dict: {"note": <str>, "severity": <str>} where
  severity is one of: "high" | "moderate" | "low" | "unknown".
  - "unknown" is the honest default when the LLM is disabled or its output
    can't be parsed/validated. We deliberately do NOT default to "low", which
    would silently mislabel everything as low-risk in a risk-monitoring tool.

Model + endpoint are read from env so they can be updated without code changes.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error

# Allowed severity values. Anything else from the model is coerced to "unknown".
VALID_SEVERITIES = {"high", "moderate", "low"}
NEUTRAL = {"note": "", "severity": "unknown"}

# Model fallback chain (unchanged rationale).
_DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-flash"]


def _model_chain():
    raw = os.environ.get("GEMINI_MODELS") or ""
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if models:
        return models
    single = (os.environ.get("GEMINI_MODEL") or "").strip()
    if single:
        return [single] + [m for m in _DEFAULT_MODELS if m != single]
    return list(_DEFAULT_MODELS)


DEFAULT_MODEL = _model_chain()[0]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_RETRYABLE_CODES = {429, 500, 502, 503, 504, 404}
_MIN_INTERVAL_S = 13.0  # >= 12s spacing to respect the confirmed 5 RPM project quota
_last_call_ts = [0.0]

# The prompt now asks for STRICT JSON with two fields. The rubric for severity
# is made explicit so the model classifies consistently.
SYSTEM_PROMPT = (
    "You are an automotive regulatory-affairs analyst. You will be given one "
    "WTO TBT/SPS notification (title, notifying member, products, objectives, "
    "and a Description of the regulatory change).\n\n"
    "Respond with a SINGLE JSON object and nothing else (no markdown, no code "
    "fences, no preamble). The object must have exactly these two keys:\n"
    '  "note": a relevance note of 2-3 sentences, based primarily on the '
    "Description, covering (1) what the regulatory change actually requires or "
    "changes, (2) how it is relevant to the automobile industry (which "
    "vehicles, components, or processes it affects), and (3) how significant it "
    "is for manufacturers/importers. Plain text only, no markdown.\n"
    '  "severity": exactly one of "high", "moderate", or "low", using this '
    "rubric:\n"
    "    - high: broad scope and/or new hardware, structural redesign, or "
    "type-approval/re-certification affecting most of the vehicle range.\n"
    "    - moderate: relabelling, reformulation, added testing or certification "
    "for a subset of products, without structural redesign.\n"
    "    - low: editorial/administrative changes, documentation, deadline "
    "extensions, or clarifications with no substantive product impact.\n\n"
    "Example of the exact output format:\n"
    '{"note": "Requires X for vehicles up to 3500 kg ... High impact because '
    'broad scope and hardware redesign.", "severity": "high"}'
)


def is_enabled():
    return bool(os.environ.get("GEMINI_API_KEY"))


def summarize(entry_text, _key=None, _model=None):
    """
    Return {"note": str, "severity": str} for the given text. Never raises.
    Returns the neutral default ({"note":"", "severity":"unknown"}) if disabled
    or if all models fail.

    Walks the model fallback chain on retryable errors; stops on first success
    or first non-retryable error.
    """
    api_key = _key if _key is not None else os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return dict(NEUTRAL)

    models = [_model] if _model else _model_chain()

    last_was_retryable = False
    for i, model in enumerate(models):
        result, retryable = _call_once(entry_text, api_key, model)
        if result is not None:
            if i > 0:
                _log(f"succeeded with fallback model '{model}' "
                     f"(after {i} model(s) failed)")
            return result
        if not retryable:
            return dict(NEUTRAL)
        last_was_retryable = True
        if i + 1 < len(models):
            _log(f"model '{model}' unavailable; trying fallback "
                 f"'{models[i + 1]}'")
    if last_was_retryable:
        _log("all models in the fallback chain were unavailable; "
             "skipping summary for this entry")
    return dict(NEUTRAL)


def _call_once(entry_text, api_key, model):
    """
    One attempt against one model. Returns (result, retryable):
      result     {"note":..., "severity":...} on success, or None on failure
      retryable  True if a DIFFERENT model might succeed.
    """
    url = f"{GEMINI_BASE}/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": entry_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 800,
            "thinkingConfig": {"thinkingBudget": 0},
            # Ask Gemini to emit raw JSON so parsing is reliable. Models that
            # don't honour this still usually return a JSON object given the
            # prompt; the parser below tolerates fences/preamble either way.
            "responseMimeType": "application/json",
        },
    }
    data = json.dumps(body).encode("utf-8")

    for attempt in range(2):  # one in-model retry, used only for 429
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
            raw_text = _extract_text(payload)
            if not raw_text:
                _log(f"empty response from model '{model}': "
                     f"{json.dumps(payload)[:300]}")
                return None, True
            parsed = _parse_structured(raw_text)
            if parsed is None:
                _log(f"could not parse JSON from model '{model}': "
                     f"{raw_text[:200]!r}")
                # A different model might format better -> retryable.
                return None, True
            return parsed, False
        except urllib.error.HTTPError as e:
            _last_call_ts[0] = time.time()
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            _log(f"HTTP {e.code} from model '{model}': {detail}")
            if e.code == 429 and attempt == 0:
                time.sleep(15)
                continue
            return None, (e.code in _RETRYABLE_CODES)
        except Exception as e:
            _last_call_ts[0] = time.time()
            _log(f"request failed for model '{model}': {type(e).__name__}: {e}")
            return None, True
    return None, True


def _parse_structured(raw_text):
    """
    Parse the model's text into {"note": str, "severity": str}, tolerating
    code fences / stray preamble. Returns None if no usable JSON object is found.
    Severity is validated against VALID_SEVERITIES; anything else -> "unknown".
    A present note with a bad severity is still salvaged (note kept, severity
    coerced to "unknown") rather than thrown away.
    """
    text = raw_text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        # after stripping backticks a leading "json" language tag may remain
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()

    obj = None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # Last resort: find the first {...} block and try that.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                obj = json.loads(text[start:end + 1])
            except (ValueError, TypeError):
                obj = None

    if not isinstance(obj, dict):
        return None

    note = obj.get("note", "")
    if not isinstance(note, str):
        note = str(note) if note is not None else ""
    note = " ".join(note.split()).strip()

    sev = obj.get("severity", "")
    sev = str(sev).strip().lower() if sev is not None else ""
    if sev not in VALID_SEVERITIES:
        sev = "unknown"

    # If there is no note at all and severity is unknown, treat as unusable.
    if not note and sev == "unknown":
        return None

    return {"note": note, "severity": sev}


def _log(msg):
    print(f"[summarizer] {msg}", file=sys.stderr)


def selftest():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return False, "GEMINI_API_KEY is not set in the environment."
    model = DEFAULT_MODEL
    out = summarize(
        "Title: Test\nDescription: A minor editorial correction to a test "
        "method, no change to thresholds.",
        _key=key, _model=model,
    )
    if out and out.get("note"):
        return True, (f"Gemini reachable via model '{model}'. "
                      f"severity={out.get('severity')!r} note={out.get('note')[:80]!r}")
    return False, (
        f"Gemini call to model '{model}' returned no usable result. "
        f"See the [summarizer] log line above for the HTTP code / reason."
    )


def _extract_text(payload):
    try:
        cand = payload["candidates"][0]
        finish = cand.get("finishReason", "")
        if finish == "MAX_TOKENS":
            _log("response hit MAX_TOKENS — output truncated; consider raising "
                 "maxOutputTokens or shortening the prompt.")
        parts = cand["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts).strip()
        return " ".join(text.split())
    except (KeyError, IndexError, TypeError):
        return ""


def list_models():
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
