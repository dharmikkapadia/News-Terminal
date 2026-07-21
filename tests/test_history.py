"""Characterization tests for history.py's dedupe/merge and JSONL round-trip."""

import history


def test_dedupe_prefers_summary_and_ts():
    items = [
        {"link": "https://x/?prid=1", "title": "A", "summary": "", "published": "", "ts": None},
        {"link": "https://x/?prid=1", "title": "A", "summary": "full body", "published": "d1", "ts": 100},
        {"link": "https://x/?prid=2", "title": "B", "summary": "b", "published": "d2", "ts": 200},
    ]
    out = history.dedupe(items)
    assert len(out) == 2
    a = next(it for it in out if it["link"].endswith("prid=1"))
    assert a["summary"] == "full body"          # summary backfilled onto the first-seen entry
    assert a["ts"] == 100                        # ts backfilled too
    assert out[0]["ts"] == 200                   # newest first


def test_dedupe_first_entry_wins_when_both_full():
    items = [
        {"link": "https://x/?prid=1", "title": "first", "summary": "s1", "published": "", "ts": 100},
        {"link": "https://x/?prid=1", "title": "second", "summary": "s2", "published": "", "ts": 999},
    ]
    out = history.dedupe(items)
    assert len(out) == 1
    assert out[0]["title"] == "first"
    assert out[0]["ts"] == 100


def test_save_load_round_trip(tmp_path):
    path = str(tmp_path / "h.jsonl")
    items = [
        {"link": "https://x/?prid=1", "title": "A", "summary": "sa", "published": "p1", "ts": 100},
        {"link": "https://x/?prid=2", "title": "B", "summary": "sb", "published": "p2", "ts": 200},
    ]
    n = history.save_file(items, path)
    assert n == 2
    back = history.load_file(path)
    assert [it["ts"] for it in back] == [100, 200]      # oldest-first on disk
    assert history.dedupe(back)[0]["ts"] == 200


def test_load_file_missing_returns_empty(tmp_path):
    assert history.load_file(str(tmp_path / "nope.jsonl")) == []


def test_load_file_skips_bad_lines(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"link": "https://x/?prid=1", "title": "A"}\nnot json\n\n', encoding="utf-8")
    items = history.load_file(str(p))
    assert len(items) == 1 and items[0]["title"] == "A"
