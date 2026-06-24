"""extractors.py — normalise raw fetched records into a uniform internal shape."""
import re
import html


def _strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_eping(raw_items, site):
    entries = []
    for item in raw_items:
        uid = str(item.get("id") or "").strip()
        if not uid:
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
