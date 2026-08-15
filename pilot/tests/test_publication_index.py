from __future__ import annotations

import datetime as dt
import json

import pytest

from sandboxer_v0.publication_index import (
    INDEX_SCHEMA,
    PublicationIndexError,
    load_index,
    newest_announcement,
    render_index_json,
    upsert_publication,
    validate_entry,
    visible_entries,
)


def _entry(**overrides):
    entry = {
        "id": "series-001-match-1",
        "title": "Laguna S 2.1 vs Muse Spark 1.2",
        "models": ["Laguna S 2.1", "Muse Spark 1.2"],
        "winner": "Laguna S 2.1",
        "report_url": "https://sandboxer.example/reports/series-001/match-1",
        "video_url": "https://www.youtube.com/watch?v=abc",
        "publish_at": "2026-08-17",
        "banner": "New Match result published: Laguna S 2.1 vs Muse Spark 1.2.",
    }
    entry.update(overrides)
    return entry


def test_validate_entry_normalizes_and_defaults_banner():
    entry = _entry()
    del entry["banner"]
    normalized = validate_entry(entry)
    assert normalized["banner"].startswith("New Match result published:")
    assert normalized["publish_at"] == "2026-08-17"


def test_validate_entry_rejects_bad_slug_and_bad_url():
    with pytest.raises(PublicationIndexError, match="PUBLICATION_ID_INVALID"):
        validate_entry(_entry(id="Series 1! / bad"))
    with pytest.raises(PublicationIndexError, match="PUBLICATION_REPORT_URL_INVALID"):
        validate_entry(_entry(report_url="javascript:alert(1)"))
    with pytest.raises(PublicationIndexError, match="PUBLICATION_PUBLISH_AT_INVALID"):
        validate_entry(_entry(publish_at="tomorrow"))


def test_upsert_replaces_by_id_and_sorts_by_publish_at(tmp_path):
    path = tmp_path / "publications.json"
    upsert_publication(path, _entry(id="b", publish_at="2026-08-19"))
    upsert_publication(path, _entry(id="a", publish_at="2026-08-17"))
    upsert_publication(path, _entry(id="b", publish_at="2026-08-21"))
    index = load_index(path)
    ids = [item["id"] for item in index["publications"]]
    assert ids == ["a", "b"]  # sorted by publish_at, no duplicate "b"


def test_visible_entries_only_reveal_past_publish_at():
    index = {
        "schema": INDEX_SCHEMA,
        "publications": [
            _entry(id="past", publish_at="2026-08-15"),
            _entry(id="future", publish_at="2026-08-19"),
            _entry(id="undated", publish_at=None),
        ],
    }
    today = dt.date(2026, 8, 17)
    assert [item["id"] for item in visible_entries(index, today)] == ["past", "undated"]
    assert newest_announcement(index, today)["id"] == "past"


def test_render_index_json_is_deterministic_and_valid():
    index = load_index(__import__("pathlib").Path("/nonexistent/never.json"))
    index["publications"] = [_entry()]
    text = render_index_json(index)
    assert json.loads(text)["schema"] == INDEX_SCHEMA
    assert render_index_json(index) == text


def test_load_missing_file_yields_empty_index():
    index = load_index(__import__("pathlib").Path("/nonexistent/never.json"))
    assert index == {"schema": INDEX_SCHEMA, "publications": []}
