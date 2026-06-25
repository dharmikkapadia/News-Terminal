#!/usr/bin/env python3
"""inspect_detail.py — TEMPORARY diagnostic. Dumps the structure of an RBI
press-release detail page so we can write an accurate body/date parser. Run via
the inspect workflow on GitHub (RBI answers the runner; it 403s this sandbox).
Delete this file and inspect.yml once the parser is written.
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
DEFAULT = "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63023"

for url in (sys.argv[1:] or [DEFAULT]):
    print("\n========== ", url)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    except Exception as ex:
        print("FETCH ERROR:", ex)
        continue
    print("STATUS", r.status_code, "bytes", len(r.content))
    soup = BeautifulSoup(r.content, "html.parser")
    print("TITLE:", (soup.title.string if soup.title else "")[:160])
    text = soup.get_text(" ", strip=True)
    for pat in (r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}",
                r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm|hrs|IST)?",
                r"\d{1,2}[-/]\d{1,2}[-/]\d{4}"):
        print("DATEPAT", repr(pat), "->", re.findall(pat, text)[:6])
    cands = []
    for el in soup.find_all(["div", "td", "p", "table", "article", "section"]):
        t = el.get_text(" ", strip=True)
        if len(t) > 150:
            cands.append((len(t), el.name, el.get("id", ""), " ".join(el.get("class", []))[:40], t[:200]))
    cands.sort(reverse=True)
    print("=== top text blocks (len, tag, id, class, snippet) ===")
    for ln, name, eid, cls, snip in cands[:10]:
        print(f"[{ln:6}] <{name} id={eid!r} class={cls!r}> {snip!r}")
