"""
fetchers.py — pluggable data fetchers, one per site "type".

Each fetcher takes a site config dict and returns a list of raw record dicts.
The engine (monitor.py) does not care HOW records are fetched; it only diffs
and reports them. To add a new kind of source, write a function here and
register it in FETCHERS at the bottom.

Currently implemented:
  - eping_api : calls the ePing azureSearch JSON API (no browser needed)

Stub for future JS-rendered sites:
  - playwright_table : renders a page and scrapes a table (lazy-imports
    Playwright so it is NOT a hard dependency unless actually used)
"""

import time
import requests

# ---------------------------------------------------------------------------
# ePing API fetcher
# ---------------------------------------------------------------------------

EPING_ENDPOINT = "https://epingalert.org/api/v1/azureSearch/getAll"

# A normal browser-ish UA. The ARRAffinity cookie from the captured cURL is an
# Azure load-balancer session token and is NOT required for public data, so we
# omit it. We send the same Referer/Accept the site uses, to be polite.
EPING_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Referer": "https://epingalert.org/en/Search/Index",
}


def fetch_eping_api(site, page_size=20, max_pages=10, timeout=30, _session=None):
    """
    Fetch notifications from the ePing azureSearch API for one site config.

    Pages through results (sorted newest-first by the API) until we have at
    least `track_limit` items or run out of pages. Returns a list of raw item
    dicts exactly as the API returns them (parsing/normalisation happens in
    extractors.py, keeping fetch and shape concerns separate).

    `_session` lets tests inject a fake requests session. In production it is
    None and a real session is created.
    """
    params = site.get("params", {})
    track_limit = site.get("track_limit", 30)

    # Build the query parameter list. requests handles list values by repeating
    # the key (countryIds=C1&countryIds=C2...), which is exactly what the API
    # expects (confirmed from the captured cURL).
    base_query = {
        "domainIds": params.get("domainIds", "1"),
        "countryIds": params.get("countryIds", []),
        "freeText": params.get("freeText", ""),
        "sortBy": params.get("sortBy", "distributionDate"),
        "sortDirection": params.get("sortDirection", "desc"),
        "language": "1",
    }

    session = _session or requests.Session()
    collected = []
    page = 1

    while page <= max_pages and len(collected) < track_limit:
        query = dict(base_query)
        query["page"] = page
        query["pageSize"] = page_size

        resp = session.get(
            EPING_ENDPOINT, params=query, headers=EPING_HEADERS, timeout=timeout
        )
        resp.raise_for_status()
        payload = resp.json()

        items = payload.get("items", []) or []
        collected.extend(items)

        total = payload.get("totalCount", len(collected))
        if len(collected) >= total or not items:
            break  # no more results to fetch

        page += 1
        time.sleep(0.5)  # be gentle with the server between pages

    # Trim to the configured track limit (newest-first ordering is preserved).
    return collected[:track_limit]


# ---------------------------------------------------------------------------
# Future: Playwright table fetcher (lazy import; only loaded if used)
# ---------------------------------------------------------------------------

def fetch_playwright_table(site, timeout=30, _session=None):
    """
    Placeholder for JS-rendered sites that have no clean JSON API.

    Renders the page with a headless browser, waits for the table selector,
    and returns a list of row dicts. Playwright is imported INSIDE the function
    so that sites which never use it don't need it installed.

    To enable: `pip install playwright && playwright install chromium`,
    then add a site to sites.json with "fetcher": "playwright_table" and a
    "selectors" block. Implement the scraping body when the first such site
    appears (we deferred this because ePing has a JSON API).
    """
    raise NotImplementedError(
        "playwright_table fetcher is a stub. ePing uses the JSON API "
        "(eping_api). Implement this when a JS-only site without an API is added."
    )


# ---------------------------------------------------------------------------
# Registry — map a site's "fetcher" string to a function
# ---------------------------------------------------------------------------

FETCHERS = {
    "eping_api": fetch_eping_api,
    "playwright_table": fetch_playwright_table,
}


def get_fetcher(name):
    if name not in FETCHERS:
        raise KeyError(
            f"Unknown fetcher '{name}'. Available: {', '.join(FETCHERS)}"
        )
    return FETCHERS[name]
