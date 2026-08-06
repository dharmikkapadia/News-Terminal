"""Regression test for the browser-render fetcher-resolution crash that took down
MarketWire rates (Scrapling browser) — see GitHub Actions run failure, 2026-08-05.

scrapling's `fetchers` package lazily imports each fetcher class on attribute access
(`scrapling/fetchers/__init__.py`'s `__getattr__`), so `from scrapling import fetchers`
can succeed while the *next* line, `getattr(fetchers, "DynamicFetcher")`, still triggers
a real import and can raise — in the wild this was an upstream browserforge bug
(`ValueError: No headers based on this input can be generated`, scrapling 0.4.12 pinning
an exact Chrome version its bundled header data doesn't cover: D4Vinci/Scrapling#396).
`rates_scrapling._render` must survive that (and any other) fetcher-resolution failure
without raising, exactly like every other scraper in this codebase — a bad render should
degrade to "kept committed snapshot", not crash the whole Action."""

import json
import sys
import types

import rates
import rates_scrapling as rs


class _CrashyFetchers:
    """Stands in for scrapling.fetchers: the module import succeeds, but resolving any
    fetcher attribute on it raises — reproducing the real browserforge crash site."""

    def __getattr__(self, name):
        raise ValueError(
            "No headers based on this input can be generated. "
            "Please relax or change some of the requirements you specified."
        )


def _install_crashy_scrapling(monkeypatch):
    fake_scrapling = types.ModuleType("scrapling")
    fake_scrapling.fetchers = _CrashyFetchers()
    monkeypatch.setitem(sys.modules, "scrapling", fake_scrapling)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fake_scrapling.fetchers)


def test_render_survives_fetcher_attribute_crash(monkeypatch):
    _install_crashy_scrapling(monkeypatch)
    html, err = rs._render("https://www.rbi.org.in/")
    assert html is None
    assert "No headers based on this input" in err


def test_fetch_rates_browser_survives_fetcher_attribute_crash(monkeypatch):
    _install_crashy_scrapling(monkeypatch)
    scraped, err = rs.fetch_rates_browser("https://www.rbi.org.in/")
    assert scraped is None
    assert err.startswith("render failed")


def test_poll_rates_browser_never_raises_and_keeps_snapshot(monkeypatch, tmp_path):
    _install_crashy_scrapling(monkeypatch)
    path = tmp_path / "rates.json"
    committed = {"policy_rates": {"repo_rate": 5.5}, "mpc": {"next_meeting_start": "2099-01-01"}}
    path.write_text(json.dumps(committed))

    status = rs.poll_rates_browser(path=str(path), home_url="https://www.rbi.org.in/",
                                    mpc_url="https://www.rbi.org.in/scripts/FS_Overview.aspx?fn=2752")

    assert status.startswith("kept committed snapshot")
    # The committed file must be untouched — a failed render never overwrites it.
    assert json.loads(path.read_text()) == committed


def test_main_never_raises_and_exits_zero(monkeypatch, tmp_path, capsys):
    _install_crashy_scrapling(monkeypatch)
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({"policy_rates": {"repo_rate": 5.5}}))
    monkeypatch.setattr(rates, "RATES_PATH", str(path))

    assert rs.main() == 0
    out = capsys.readouterr().out
    assert "No headers based on this input" in out or "kept committed snapshot" in out
