"""
extractors.py — normalise raw fetched records into a uniform internal shape.

Every site, no matter how it's fetched, gets reduced to a list of "entries"
with the SAME keys, so the diff engine and report template never need to know
site-specific field names. To add a site with a different payload shape, write
a new extractor function and register it in EXTRACTORS.

Uniform entry shape (the contract the rest of the app relies on):
{
    "uid":        str,   # stable unique id used for change detection
    "title":      str,   # human-readable headline
    "summary":    str,   # short description (plain text)
    "meta":       dict,  # display fields: country, date, deadline, etc.
    "link":       str,   # primary "more information" URL
    "extra_link": str,   # optional secondary URL (e.g. direct PDF), or ""
    "llm_input":  str,   # condensed text fed to the LLM for a relevance note
}
"""

import re
import html


def _strip_html(text):
    """Remove tags and unescape entities. ePing gives <p>..</p> wrapped text."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_eping(raw_items, site):
    """
    Turn raw ePing API items into uniform entries.

    - uid: the API's own "id" (clean primary key; better than the messy
      comma-joined documentSymbol).
    - link: linkToNotification (the WTO document landing page).
    - extra_link: notifiedDocumentLink (direct PDF) when present.
    - prefers the *Plain fields the API already provides, falling back to
      stripping HTML from the rich fields if a Plain variant is missing.
    """
    entries = []
    for item in raw_items:
        uid = str(item.get("id") or "").strip()
        if not uid:
            # Without a stable id we can't track changes reliably; skip.
            continue

        title = item.get("titlePlain") or _strip_html(item.get("title"))
        summary = item.get("descriptionPlain") or _strip_html(item.get("description"))
        products = item.get("productsFreeTextPlain") or _strip_html(
            item.get("productsFreeText")
        )

        symbols = (item.get("documentSymbol") or "").strip()
        objectives = ", ".join(
            o.get("name", "") for o in (item.get("objectives") or [])
        )

        meta = {
            "Notifying member": item.get("notifyingMember") or "",
            "Area": item.get("area") or "",
            "Distribution date": _date_only(item.get("distributionDate")),
            "Comment deadline": _date_only(item.get("commentDeadlineDate")),
            "Document symbol(s)": symbols,
            "Products": products,
            "Objectives": objectives,
        }

        link = (item.get("linkToNotification") or "").strip()
        extra_link = (item.get("notifiedDocumentLink") or "").strip()

        # Condensed, clean text block for the LLM relevance note.
        llm_input = (
            f"Title: {title}\n"
            f"Notifying member: {meta['Notifying member']}\n"
            f"Products: {products}\n"
            f"Objectives: {objectives}\n"
            f"Description: {summary}"
        ).strip()

        entries.append(
            {
                "uid": uid,
                "title": title or "(untitled notification)",
                "summary": summary,
                "meta": meta,
                "link": link,
                "extra_link": extra_link,
                "llm_input": llm_input,
            }
        )
    return entries


def _date_only(iso_str):
    """'2026-06-02T00:00:00+00:00' -> '2026-06-02'. Safe on None/garbage."""
    if not iso_str:
        return ""
    return str(iso_str)[:10]


EXTRACTORS = {
    "eping_api": extract_eping,
}


def get_extractor(name):
    if name not in EXTRACTORS:
        raise KeyError(
            f"Unknown extractor '{name}'. Available: {', '.join(EXTRACTORS)}"
        )
    return EXTRACTORS[name]
