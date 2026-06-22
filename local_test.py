"""
local_test.py — validate the pipeline against the real uploaded ePing JSON
WITHOUT network access. We wrap the user's sample file in the same envelope the
API returns and inject it via a fake requests session, so the genuine fetcher,
extractor, diff, and report code all run.

Run: python local_test.py
"""

import json
import os
import shutil
import fetchers
import extractors
import summarizer
import monitor

SAMPLE = "/mnt/user-data/uploads/eping_API_response_1.json"
TMP = "/home/claude/eping_monitor/_test"


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class FakeSession:
    """Returns the sample payload on first page, empty on subsequent pages."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0
    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        if self.calls == 1:
            return FakeResp(self._payload)
        # Pretend there are no further pages.
        empty = dict(self._payload)
        empty["items"] = []
        return FakeResp(empty)


def load_sample():
    with open(SAMPLE, "r", encoding="utf-8") as f:
        return json.load(f)


def test_fetch_and_extract():
    payload = load_sample()
    # The sample has pageSize 1 / one item; bump totalCount so paging stops cleanly.
    payload["totalCount"] = len(payload["items"])
    site = {
        "id": "eping_gcc_43020", "name": "ePing test",
        "fetcher": "eping_api", "track_limit": 30,
        "params": {"domainIds": "1", "countryIds": ["C048"], "freeText": "43.020"},
    }
    fake = FakeSession(payload)
    raw = fetchers.fetch_eping_api(site, _session=fake)
    assert len(raw) == 1, f"expected 1 raw item, got {len(raw)}"

    entries = extractors.extract_eping(raw, site)
    assert len(entries) == 1
    e = entries[0]

    print("--- extracted entry ---")
    print("uid:        ", e["uid"])
    print("title:      ", e["title"])
    print("link:       ", e["link"])
    print("extra_link: ", e["extra_link"])
    print("meta:")
    for k, v in e["meta"].items():
        print(f"    {k}: {v}")
    print("llm_input (first 200 chars):")
    print("   ", e["llm_input"][:200].replace("\n", " | "))

    # Assertions on the contract
    assert e["uid"] == "113917"
    assert e["title"].startswith("GCC Technical regulation")
    assert "<" not in e["title"] and "<" not in e["summary"], "HTML not stripped"
    assert e["link"].startswith("https://docs.wto.org/"), "wrong primary link"
    assert e["extra_link"].startswith("https://members.wto.org/"), "wrong pdf link"
    assert e["meta"]["Notifying member"] == "Bahrain, Kingdom of"
    assert e["meta"]["Distribution date"] == "2026-06-02"
    assert e["meta"]["Comment deadline"] == "2026-08-01"
    print("\n[PASS] fetch + extract contract\n")
    return payload, site


def test_baseline_then_new(payload, site):
    """First run = baseline (0 new). Second run with an added item = 1 new."""
    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    state_path = os.path.join(TMP, "state.json")
    report_path = os.path.join(TMP, "report.html")

    # Monkeypatch the fetcher to use our fake session with the current payload.
    holder = {"payload": payload}
    orig = fetchers.fetch_eping_api
    def patched(s, **kw):
        return orig(s, _session=FakeSession(holder["payload"]))
    fetchers.FETCHERS["eping_api"] = patched

    cfg = {"sites": [site]}
    cfg_path = os.path.join(TMP, "sites.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)

    # ---- Run 1: baseline ----
    import subprocess, sys
    def run():
        return subprocess.run(
            [sys.executable, "monitor.py", "--sites", cfg_path,
             "--state", state_path, "--report", report_path, "--no-llm"],
            cwd="/home/claude/eping_monitor", capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "/home/claude/eping_monitor"},
        )
    # We can't easily monkeypatch across a subprocess, so instead call in-process:
    fetchers.FETCHERS["eping_api"] = patched
    prior = monitor.load_json(state_path, {})
    rep1, st1 = monitor.process_site(site, prior, use_llm=False)
    assert rep1["is_baseline"] is True
    assert len(rep1["new_entries"]) == 0
    assert st1["uids"] == ["113917"]
    monitor.save_json(state_path, {site["id"]: st1})
    print("[PASS] run 1 baseline: 0 new, tracked", st1["tracked_count"])

    # ---- Run 2: add a brand-new notification ----
    payload2 = json.loads(json.dumps(payload))  # deep copy
    new_item = json.loads(json.dumps(payload["items"][0]))
    new_item["id"] = "999999"
    new_item["titlePlain"] = "NEW TEST notification for road vehicles"
    new_item["title"] = "<p>NEW TEST notification for road vehicles</p>"
    payload2["items"] = [new_item] + payload["items"]  # newest first
    payload2["totalCount"] = len(payload2["items"])
    holder["payload"] = payload2

    prior = monitor.load_json(state_path, {})
    rep2, st2 = monitor.process_site(site, prior, use_llm=False)
    assert rep2["is_baseline"] is False
    assert len(rep2["new_entries"]) == 1, rep2["new_entries"]
    assert rep2["new_entries"][0]["uid"] == "999999"
    assert st2["uids"][0] == "999999" and "113917" in st2["uids"]
    print("[PASS] run 2: detected", len(rep2["new_entries"]), "new ->",
          rep2["new_entries"][0]["title"])

    # ---- Render the report for visual sanity ----
    monitor.render_report([rep2], report_path)
    assert os.path.exists(report_path)
    size = os.path.getsize(report_path)
    print("[PASS] report rendered:", report_path, f"({size} bytes)")

    fetchers.FETCHERS["eping_api"] = orig  # restore
    return report_path


if __name__ == "__main__":
    payload, site = test_fetch_and_extract()
    report_path = test_baseline_then_new(payload, site)
    print("\nAll local tests passed.")
