"""history.py — durable press-release history kept as JSON-lines in the repo.

The poller (poll.py, run by the GitHub Action) reads + writes data/history.jsonl
and the Action commits it, so history lives in the repo with no external DB. The
Streamlit app reads it back (a committed file, or a raw URL via MARKETWIRE_HISTORY_URL)
and merges it with the live feed.

JSONL is used on purpose: it's append-friendly (we write oldest-first), so each
update is a small git diff rather than a rewritten binary blob.
"""

import json
import os
import re

import requests

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HISTORY_PATH = os.environ.get(
    "MARKETWIRE_HISTORY_FILE",
    os.path.join(_DATA_DIR, "history.jsonl"),
)
NOTIFICATIONS_PATH = os.environ.get(
    "MARKETWIRE_NOTIFICATIONS_FILE",
    os.path.join(_DATA_DIR, "notifications.jsonl"),
)
SEBI_PUBLIC_ISSUES_PATH = os.environ.get(
    "MARKETWIRE_SEBI_PUBLIC_ISSUES_FILE",
    os.path.join(_DATA_DIR, "sebi_public_issues.jsonl"),
)
UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
_FIELDS = ("link", "title", "summary", "published", "ts")


def _key(it):
    """Stable identity: RBI press-release `prid` or notification `Id` from the
    link, else the link. (The two feeds are stored separately, so a prid and an
    Id sharing a number never clash.)"""
    link = it.get("link", "") or ""
    m = re.search(r"\bprid=(\d+)", link, re.I) or re.search(r"\bid=(\d+)", link, re.I)
    return m.group(1) if m else link


def dedupe(items):
    """Merge by key (prefer entries that carry a summary / a date); newest first."""
    best = {}
    for it in items:
        k = _key(it)
        if not k:
            continue
        cur = best.get(k)
        if cur is None:
            best[k] = dict(it)
        else:
            if not cur.get("summary") and it.get("summary"):
                cur["summary"] = it["summary"]
            if cur.get("ts") is None and it.get("ts") is not None:
                cur["ts"] = it["ts"]
                cur["published"] = it.get("published") or cur.get("published", "")
    out = list(best.values())
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out


def _parse_lines(lines):
    items = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def load_file(path=HISTORY_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _parse_lines(f)
    except FileNotFoundError:
        return []


def save_file(items, path=HISTORY_PATH):
    """Write deduped history oldest-first (append-friendly diffs). Returns count."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    items = list(reversed(dedupe(items)))  # oldest-first on disk
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps({k: it.get(k) for k in _FIELDS}, ensure_ascii=False) + "\n")
    return len(items)


def load_durable(source=None, url_env="MARKETWIRE_HISTORY_URL", default_path=HISTORY_PATH):
    """Read durable history for the app. `source` (or the `url_env` env var) may be
    a raw http(s) URL or a file path; defaults to `default_path` (the local
    committed file). Pass the notifications path/env to read that feed instead.
    Never raises — returns [] on any problem."""
    source = source or os.environ.get(url_env, "").strip() or default_path
    if source.startswith(("http://", "https://")):
        try:
            resp = requests.get(source, headers={"User-Agent": UA}, timeout=15)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return _parse_lines(resp.text.splitlines())
        except Exception:
            return []
    return load_file(source)
