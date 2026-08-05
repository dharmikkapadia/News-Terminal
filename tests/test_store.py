"""Characterization tests for store.py's sqlite backend — upsert semantics and the
concurrent-writer race that used to crash the app with a UNIQUE constraint error.
"""

import os
import tempfile

import pytest

store = pytest.importorskip("store")


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MARKETWIRE_DB", path)
    store.init_db()
    yield
    try:
        os.remove(path)
    except OSError:
        pass


def test_upsert_counts_only_genuinely_new_rows(db):
    item = {"key": "te:a", "link": "https://x/a", "title": "A", "summary": "", "ts": 100}
    assert store.upsert([item], category="te_world_news") == 1
    assert store.upsert([item], category="te_world_news") == 0    # already stored
    assert store.count(category="te_world_news") == 1


def test_upsert_backfills_a_missing_summary_without_touching_an_existing_one(db):
    bare = {"key": "te:a", "link": "https://x/a", "title": "A", "summary": "", "ts": 100}
    store.upsert([bare], category="te_world_news")
    store.upsert([{**bare, "summary": "filled in later"}], category="te_world_news")
    row = store.load(category="te_world_news")[0]
    assert row["summary"] == "filled in later"
    # A second, different summary must NOT clobber the one already stored.
    store.upsert([{**bare, "summary": "a different summary"}], category="te_world_news")
    assert store.load(category="te_world_news")[0]["summary"] == "filled in later"


def test_upsert_survives_two_writers_racing_on_the_same_new_key(db):
    """Regression test: store.upsert used to SELECT-then-INSERT, so two concurrent
    Streamlit sessions polling the same feed could both see a key as missing and
    both try to INSERT it — the second writer's connection raised
    sqlite3.IntegrityError: UNIQUE constraint failed and crashed the page. The
    fix (INSERT ... ON CONFLICT DO UPDATE) makes the second writer a no-op.

    Exercises the real upsert() code path from two OS threads (not two sequential
    calls), synchronized with a barrier so both are racing on the same brand-new
    key rather than running one strictly after the other."""
    import threading

    item = {"key": "te:race", "link": "https://x/race", "title": "Race", "summary": "", "ts": 200}
    barrier = threading.Barrier(2)
    errors = []

    def writer():
        barrier.wait()
        try:
            store.upsert([item], category="te_world_news")
        except Exception as ex:                             # pragma: no cover - failure path
            errors.append(ex)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"upsert raised under a concurrent race: {errors}"
    assert store.count(category="te_world_news") == 1
