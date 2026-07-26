"""App smoke test: run streamlit_app.py headlessly via AppTest and assert it doesn't
crash. Skipped where streamlit/feedparser aren't installable (e.g. the dev sandbox);
runs on any host with the full requirements installed."""

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("feedparser")


def test_app_runs_without_exception():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("streamlit_app.py", default_timeout=60)
    at.run()
    assert not at.exception


@pytest.mark.parametrize("layout", ["Stream", "Grid"])
@pytest.mark.parametrize("sources", ["TE India News,TE World News", "Press Releases", ""])
def test_app_runs_for_each_layout_and_source_selection(layout, sources):
    """Covers the scraped feeds' wiring (FEEDS -> _FETCHERS) in both layouts, and
    the no-sources-selected branch, without needing any feed to be reachable."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("streamlit_app.py", default_timeout=90)
    at.query_params["layout"] = layout
    at.query_params["sources"] = sources
    at.run()
    assert not at.exception
