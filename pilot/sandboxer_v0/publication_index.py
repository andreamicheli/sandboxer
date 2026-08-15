"""Static-site publication index for the broadcast layer.

The site is a static deploy: "scheduling" an article for a future publication
day means storing it with a ``publish_at`` date and revealing it (banner +
homepage listing) only once that date has arrived.  This module owns the
index schema and the deterministic, atomic upsert so the pipeline can add a
result when the video uploads, without it appearing on the homepage early.

The date-gating itself is checked twice: here (Python, for tests and the
operator CLI) and client-side in ``site/home.js`` (for the actual reveal).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

INDEX_SCHEMA = "sandboxer.publication-index.v1"
_SLUG = re.compile(r"^[a-z0-9-]{1,80}$")
_URL = re.compile(r"^https?://")


class PublicationIndexError(ValueError):
    """A publication index entry or file cannot be read or written safely."""


def _iso_today(today: dt.date | None) -> dt.date:
    return today or dt.date.today()


def validate_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one publication entry and return a normalized copy."""
    if not isinstance(entry, Mapping):
        raise PublicationIndexError("PUBLICATION_ENTRY_INVALID")
    slug = str(entry.get("id", ""))
    if not _SLUG.fullmatch(slug):
        raise PublicationIndexError("PUBLICATION_ID_INVALID")
    title = str(entry.get("title", "")).strip()
    if not title:
        raise PublicationIndexError("PUBLICATION_TITLE_MISSING")
    report_url = str(entry.get("report_url", "")).strip()
    if report_url and not _URL.match(report_url):
        raise PublicationIndexError("PUBLICATION_REPORT_URL_INVALID")
    video_url = str(entry.get("video_url", "")).strip()
    if video_url and not _URL.match(video_url):
        raise PublicationIndexError("PUBLICATION_VIDEO_URL_INVALID")
    publish_at = str(entry.get("publish_at", "")).strip()
    try:
        parsed = dt.date.fromisoformat(publish_at) if publish_at else None
    except ValueError as error:
        raise PublicationIndexError("PUBLICATION_PUBLISH_AT_INVALID") from error
    models = [str(item).strip() for item in entry.get("models", ()) if str(item).strip()]
    winner = str(entry.get("winner", "")).strip() or None
    banner = str(entry.get("banner", "")).strip() or f"New Match result published: {title}."
    return {
        "id": slug,
        "title": title[:200],
        "models": models,
        "winner": winner,
        "report_url": report_url or None,
        "video_url": video_url or None,
        "publish_at": publish_at or None,
        "banner": banner[:280],
    }


def load_index(path: Path) -> dict[str, Any]:
    """Load the index; an absent file yields an empty index."""
    if not path.exists():
        return {"schema": INDEX_SCHEMA, "publications": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationIndexError("PUBLICATION_INDEX_UNREADABLE") from error
    if not isinstance(data, Mapping) or data.get("schema") != INDEX_SCHEMA:
        raise PublicationIndexError("PUBLICATION_INDEX_SCHEMA_UNSUPPORTED")
    publications = data.get("publications")
    if not isinstance(publications, list):
        raise PublicationIndexError("PUBLICATION_INDEX_SCHEMA_UNSUPPORTED")
    return {"schema": INDEX_SCHEMA, "publications": [validate_entry(item) for item in publications]}


def upsert_publication(path: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically insert or replace one publication by ``id`` and return the index."""
    normalized = validate_entry(entry)
    index = load_index(path)
    slug = normalized["id"]
    index["publications"] = [item for item in index["publications"] if item["id"] != slug]
    index["publications"].append(normalized)
    index["publications"].sort(key=lambda item: (item["publish_at"] or "", item["id"]))
    _atomic_write(path, index)
    return index


def visible_entries(index: Mapping[str, Any], today: dt.date | None = None) -> list[dict[str, Any]]:
    """Entries whose ``publish_at`` has arrived (or that have no date)."""
    if index.get("schema") != INDEX_SCHEMA:
        raise PublicationIndexError("PUBLICATION_INDEX_SCHEMA_UNSUPPORTED")
    now = _iso_today(today)
    result: list[dict[str, Any]] = []
    for item in index.get("publications", ()):
        publish_at = item.get("publish_at")
        if not publish_at or dt.date.fromisoformat(publish_at) <= now:
            result.append(item)
    return result


def newest_announcement(index: Mapping[str, Any], today: dt.date | None = None) -> dict[str, Any] | None:
    """The most recent visible publication for the top-of-page banner, if any."""
    visible = visible_entries(index, today)
    if not visible:
        return None
    return max(visible, key=lambda item: (item.get("publish_at") or "", item["id"]))


def _atomic_write(path: Path, index: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(index, sort_keys=True, separators=(",", ":"), indent=2)
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def render_index_json(index: Mapping[str, Any]) -> str:
    """Canonical JSON for the site data file."""
    if index.get("schema") != INDEX_SCHEMA:
        raise PublicationIndexError("PUBLICATION_INDEX_SCHEMA_UNSUPPORTED")
    return json.dumps(index, sort_keys=True, separators=(",", ":"), indent=2)


__all__ = [
    "INDEX_SCHEMA",
    "PublicationIndexError",
    "load_index",
    "newest_announcement",
    "render_index_json",
    "upsert_publication",
    "validate_entry",
    "visible_entries",
]
