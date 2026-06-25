#!/usr/bin/env python3
"""inspect_detail.py — TEMPORARY diagnostic. Run via the inspect workflow on
GitHub (RBI answers the runner; it 403s this sandbox). Delete after use.
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; MarketWire/1.0; RSS reader)"
DEFAULT = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"

for url in (sys.argv[1:] or [DEFAULT]):
    print("\n========== ", url)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    except Exception as ex:
        print("FETCH ERROR:", ex)
        continue
    print("STATUS", r.status_code, "bytes", len(r.content))
    soup = BeautifulSoup(r.content, "html.parser")
    print("TITLE:", (soup.title.string if soup.title else "")[:140])

    anchors = soup.find_all("a")
    href_prid = [a for a in anchors if "prid" in (a.get("href") or "").lower()]
    onclick_prid = [a for a in anchors if "prid" in (a.get("onclick") or "").lower()]
    print(f"anchors total={len(anchors)} | href~prid={len(href_prid)} | onclick~prid={len(onclick_prid)}")
    for a in href_prid[:4]:
        print("  HREF   ", repr((a.get('href') or ''))[:100], "| txt:", a.get_text(' ', strip=True)[:55])
    for a in onclick_prid[:4]:
        print("  ONCLICK", repr((a.get('onclick') or ''))[:120], "| txt:", a.get_text(' ', strip=True)[:55])
    longt = [(a.get_text(' ', strip=True), a.get('href'), a.get('onclick')) for a in anchors
             if len(a.get_text(' ', strip=True)) > 35]
    print(f"=== anchors with long text (likely titles): {len(longt)} ===")
    for t, h, o in longt[:6]:
        print(f"  TXT {t[:55]!r} HREF {str(h)[:55]!r} ONCLICK {str(o)[:55]!r}")
    # any table that looks like a release listing
    print("=== tables with prid links ===")
    for tb in soup.find_all("table"):
        n = len([a for a in tb.find_all('a') if 'prid' in ((a.get('href') or '') + (a.get('onclick') or '')).lower()])
        if n:
            print(f"  <table id={tb.get('id','')!r} class={' '.join(tb.get('class',[]))!r}> prid-links={n}")
