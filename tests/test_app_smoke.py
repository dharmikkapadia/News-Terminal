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
