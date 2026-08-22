"""Hard publication gate for the broadcast handoff.

A YouTube upload must never go out without its generated Series Result
Report.  The last ungated run produced a broadcast record with
``indexed=false``, ``slug=null`` and a ``report_url`` pointing at the source
repository instead of a real report page.  This module owns the gate that
makes that state impossible to publish silently: every blocking rule is
checked, all failures are collected, and the caller (the YouTube upload
path) refuses to proceed while any failure remains.

The gate is fail-closed: missing evidence is itself a blocking failure.
``allow_ungated=True`` at the call site is the only deliberate escape hatch.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

BUNDLE_SCHEMA = "sandboxer.evidence-bundle.v1"
INDEX_SCHEMA = "sandboxer.publication-index.v1"
USER_AGENT = "sandboxer-publish-gate/1"

_PLACEHOLDER_HOSTS = {
    "example.com",
    "www.example.com",
    "example.org",
    "www.example.org",
    "example.net",
    "www.example.net",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
}
_REPO_HOSTS = {
    "github.com",
    "www.github.com",
    "gitlab.com",
    "www.gitlab.com",
    "bitbucket.org",
    "www.bitbucket.org",
}
_REQUIRED_BUNDLE_KEYS = {
    "schema_version",
    "version",
    "url",
    "previous_version_url",
    "previous_bundle_hash",
    "provenance",
    "public",
    "restricted",
    "checksums",
    "bundle_hash",
    "signature",
}


class PublishGateError(ValueError):
    """A real upload was refused because the publication gate is unsatisfied."""


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80]


def _series_slug(bundle: Mapping[str, Any]) -> str:
    public = bundle.get("public")
    specification = public.get("specification") if isinstance(public, Mapping) else None
    series_id = specification.get("series_id") if isinstance(specification, Mapping) else None
    slug = _slugify(str(series_id)) if series_id else ""
    return slug or f"series-{str(bundle.get('bundle_hash', ''))[:12]}"


def load_evidence_bundle(bundle_path: Path | str) -> dict[str, Any]:
    """Read an evidence bundle from disk or raise ``PublishGateError``."""
    path = Path(bundle_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise PublishGateError("PUBLISH_GATE_BUNDLE_MISSING")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishGateError("PUBLISH_GATE_BUNDLE_UNREADABLE") from error
    if not isinstance(bundle, dict):
        raise PublishGateError("PUBLISH_GATE_BUNDLE_UNREADABLE")
    return bundle


def _bundle_failure(bundle: Mapping[str, Any]) -> str | None:
    if bundle.get("schema_version") != BUNDLE_SCHEMA:
        return "PUBLISH_GATE_BUNDLE_INVALID:schema_version"
    if missing := _REQUIRED_BUNDLE_KEYS - set(bundle):
        return "PUBLISH_GATE_BUNDLE_INCOMPLETE:" + ",".join(sorted(missing))
    version = bundle.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return "PUBLISH_GATE_BUNDLE_INVALID:version"
    public = bundle.get("public")
    specification = public.get("specification") if isinstance(public, Mapping) else None
    series_id = specification.get("series_id") if isinstance(specification, Mapping) else None
    if not isinstance(series_id, str) or not series_id.strip():
        return "PUBLISH_GATE_BUNDLE_SERIES_ID_MISSING"
    return None


def report_url_failure(report_url: Any) -> str | None:
    """Rule 2: reject missing, non-web, placeholder and bare-repository URLs."""
    url = str(report_url or "").strip()
    if not url:
        return "PUBLISH_GATE_REPORT_URL_MISSING"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"PUBLISH_GATE_REPORT_URL_NOT_WEB:{parsed.scheme or 'none'}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "PUBLISH_GATE_REPORT_URL_MISSING"
    if host in _PLACEHOLDER_HOSTS or host.endswith((".example", ".local")) or "placeholder" in host:
        return f"PUBLISH_GATE_REPORT_URL_PLACEHOLDER:{host}"
    path = parsed.path.rstrip("/")
    depth = len([segment for segment in path.split("/") if segment])
    if path.endswith(".git") or (host in _REPO_HOSTS and depth < 3):
        return f"PUBLISH_GATE_REPORT_URL_BARE_REPOSITORY:{url}"
    return None


def check_report_url_http(report_url: str, *, timeout: float = 10.0) -> int | None:
    """Return the HTTP status of ``report_url`` (HEAD, ranged-GET fallback), or ``None``.

    Some hosts reject HEAD with 403/405/501; a follow-up GET request probes
    those without assuming a HEAD-capable server.  Transport failures yield
    ``None`` so the gate can report unreachability as a blocking failure.
    """
    headers = {"User-Agent": USER_AGENT}
    request = urllib.request.Request(report_url, method="HEAD", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        status = error.code
        if status not in (403, 405, 501):
            return status
    except (urllib.error.URLError, OSError, ValueError):
        return None
    request = urllib.request.Request(report_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def index_entries(publications_index: Any) -> list[Any] | None:
    """Accept a loaded index, a bare entry list, or a path to publications.json."""
    if publications_index is None:
        return None
    if isinstance(publications_index, (str, Path)):
        try:
            data = json.loads(Path(publications_index).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return index_entries(data)
    if isinstance(publications_index, Mapping):
        entries = publications_index.get("publications")
        return list(entries) if isinstance(entries, (list, tuple)) else None
    if isinstance(publications_index, (list, tuple)):
        return list(publications_index)
    return None


def check_publish_gate(
    bundle_path: Path | str | None,
    report_url: str | None,
    publications_index: Any,
    *,
    require_http: bool = True,
    http_timeout: float = 10.0,
) -> list[str]:
    """Collect every blocking publication failure; an empty list means pass.

    Rules: (1) the frozen evidence bundle must exist and pass basic shape
    validation, yielding the site slug from its series id; (2) ``report_url``
    must be a real web page, not a placeholder host or a bare repository URL;
    (3) with ``require_http=True`` it must answer HTTP 200 right now (set it
    to False in offline tests); (4) the publications index — a loaded index,
    an entry list, or a path to ``publications.json`` — must already list that
    slug, and an explicit ``indexed=false`` on the entry blocks too.  An
    entry listed without an ``indexed`` field counts as indexed, matching the
    static-site index written by ``publication_index.upsert_publication``.
    """
    failures: list[str] = []

    slug: str | None = None
    bundle_problem: str | None = None
    if bundle_path is None or not Path(bundle_path).is_file():
        bundle_problem = "PUBLISH_GATE_BUNDLE_MISSING"
    else:
        try:
            bundle = load_evidence_bundle(bundle_path)
        except PublishGateError as error:
            bundle_problem = str(error)
        else:
            bundle_problem = _bundle_failure(bundle)
    if bundle_problem:
        failures.append(bundle_problem)
    else:
        slug = _series_slug(bundle)

    url_failure = report_url_failure(report_url)
    if url_failure:
        failures.append(url_failure)
    elif require_http:
        status = check_report_url_http(str(report_url).strip(), timeout=http_timeout)
        if status is None:
            failures.append("PUBLISH_GATE_REPORT_URL_UNREACHABLE")
        elif status != 200:
            failures.append(f"PUBLISH_GATE_REPORT_URL_HTTP_{status}")

    entries = index_entries(publications_index)
    if entries is None:
        failures.append("PUBLISH_GATE_INDEX_MISSING")
    else:
        matched = [entry for entry in entries if isinstance(entry, Mapping) and str(entry.get("id", "")) == slug]
        if not matched:
            failures.append(f"PUBLISH_GATE_INDEX_ENTRY_MISSING:{slug or 'unknown'}")
        elif any(entry.get("indexed") is False for entry in matched):
            failures.append(f"PUBLISH_GATE_INDEX_NOT_INDEXED:{slug}")

    return failures


__all__ = [
    "BUNDLE_SCHEMA",
    "INDEX_SCHEMA",
    "PublishGateError",
    "check_publish_gate",
    "check_report_url_http",
    "index_entries",
    "load_evidence_bundle",
    "report_url_failure",
]
