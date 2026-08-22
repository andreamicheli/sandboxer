from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from sandboxer_v0.publish_gate import (
    PublishGateError,
    check_publish_gate,
    check_report_url_http,
    report_url_failure,
)
from sandboxer_v0.youtube import FakeYoutubeService, YoutubeUploader

SLUG = "series-001-match-1"
REPORT_URL = f"https://broadcast.sandboxer.dev/assets/reports/{SLUG}/narrative.html"


def _bundle_dict(series_id: str = "Series 001 Match 1") -> dict:
    return {
        "schema_version": "sandboxer.evidence-bundle.v1",
        "version": 1,
        "url": f"sandboxer://evidence/{series_id}/v1",
        "previous_version_url": None,
        "previous_bundle_hash": None,
        "provenance": [f"sandboxer://evidence/{series_id}/v1"],
        "public": {"specification": {"series_id": series_id}},
        "restricted": {},
        "checksums": {},
        "bundle_hash": "a" * 64,
        "signature": "b" * 64,
    }


def _write_bundle(tmp_path: Path, *, raw: str | None = None, **overrides) -> Path:
    path = tmp_path / "evidence.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(_bundle_dict(**overrides)), encoding="utf-8")
    return path


def _index_entry(**overrides) -> dict:
    entry = {"id": SLUG, "title": "A vs B", "report_url": REPORT_URL}
    entry.update(overrides)
    return entry


def _loaded_index(**entry_overrides) -> dict:
    return {"schema": "sandboxer.publication-index.v1", "publications": [_index_entry(**entry_overrides)]}


def _video(tmp_path: Path) -> Path:
    video = tmp_path / "delivery.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" * 64)
    return video


def _response(status: int):
    context = mock.MagicMock()
    context.__enter__.return_value.status = status
    return context


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(REPORT_URL, status, "probe", hdrs=None, fp=None)


# --- Rule 1: the evidence bundle must exist and pass basic validation.


def test_gate_passes_for_valid_bundle_report_and_index(tmp_path):
    failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index(), require_http=False)
    assert failures == []


def test_missing_bundle_blocks_and_leaves_index_unsatisfiable(tmp_path):
    failures = check_publish_gate(tmp_path / "nope.json", REPORT_URL, _loaded_index(), require_http=False)
    assert failures == ["PUBLISH_GATE_BUNDLE_MISSING", "PUBLISH_GATE_INDEX_ENTRY_MISSING:unknown"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("<not json", "PUBLISH_GATE_BUNDLE_UNREADABLE"),
        ('{"schema_version": "other"}', "PUBLISH_GATE_BUNDLE_INVALID:schema_version"),
        (json.dumps({**_bundle_dict(), "version": 0}), "PUBLISH_GATE_BUNDLE_INVALID:version"),
        (json.dumps({k: v for k, v in _bundle_dict().items() if k != "signature"}), "PUBLISH_GATE_BUNDLE_INCOMPLETE:signature"),
        (json.dumps({**_bundle_dict(), "public": {}}), "PUBLISH_GATE_BUNDLE_SERIES_ID_MISSING"),
        (json.dumps([_bundle_dict()]), "PUBLISH_GATE_BUNDLE_UNREADABLE"),
    ],
)
def test_invalid_bundles_fail_with_specific_codes(tmp_path, raw, code):
    failures = check_publish_gate(_write_bundle(tmp_path, raw=raw), REPORT_URL, _loaded_index(), require_http=False)
    assert code in failures


def test_empty_bundle_file_blocks(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    failures = check_publish_gate(path, REPORT_URL, _loaded_index(), require_http=False)
    assert "PUBLISH_GATE_BUNDLE_MISSING" in failures


def test_slug_is_derived_from_the_bundle_series_id(tmp_path):
    bundle = _write_bundle(tmp_path, series_id="Best-of-3 / Match! 001")
    failures = check_publish_gate(bundle, REPORT_URL, {"publications": [{"id": "best-of-3-match-001"}]}, require_http=False)
    assert failures == []


# --- Rule 2: report_url must be a real page, not a placeholder or repo URL.


@pytest.mark.parametrize(
    "url",
    [
        "https://sandboxer.example/assets/reports/x/narrative.html",
        "https://example.com/reports/x",
        "https://localhost:8000/report",
        "https://placeholder.example.org/page",
    ],
)
def test_placeholder_hosts_block(url):
    assert report_url_failure(url).startswith("PUBLISH_GATE_REPORT_URL_PLACEHOLDER:")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/SandBoxer/SandBoxer",
        "https://github.com/SandBoxer/SandBoxer/",
        "https://gitlab.com/group/project.git",
        "https://bitbucket.org/org/repo",
    ],
)
def test_bare_repository_urls_block(url):
    assert report_url_failure(url).startswith("PUBLISH_GATE_REPORT_URL_BARE_REPOSITORY:")


def test_deep_documentation_page_on_a_repo_host_is_not_bare():
    url = "https://github.com/SandBoxer/SandBoxer/blob/main/site/assets/reports/s/narrative.html"
    assert report_url_failure(url) is None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (None, "PUBLISH_GATE_REPORT_URL_MISSING"),
        ("", "PUBLISH_GATE_REPORT_URL_MISSING"),
        ("sandboxer://reports/x/v1", "PUBLISH_GATE_REPORT_URL_NOT_WEB:sandboxer"),
        ("ftp://broadcast.sandboxer.dev/r", "PUBLISH_GATE_REPORT_URL_NOT_WEB:ftp"),
    ],
)
def test_missing_or_non_web_report_urls_block(url, code):
    assert report_url_failure(url) == code


# --- Rule 3: report_url must answer HTTP 200 when required (mocked here).


def test_require_http_true_accepts_a_live_200_page(tmp_path):
    with mock.patch("sandboxer_v0.publish_gate.urllib.request.urlopen", return_value=_response(200)) as urlopen:
        failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index())
    assert failures == []
    request = urlopen.call_args.args[0]
    assert request.get_method() == "HEAD"
    assert request.full_url == REPORT_URL


@pytest.mark.parametrize("status", [404, 500, 503])
def test_non_200_status_blocks(tmp_path, status):
    with mock.patch("sandboxer_v0.publish_gate.urllib.request.urlopen", side_effect=_http_error(status)):
        failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index())
    assert f"PUBLISH_GATE_REPORT_URL_HTTP_{status}" in failures


def test_transport_failure_reports_unreachable(tmp_path):
    with mock.patch("sandboxer_v0.publish_gate.urllib.request.urlopen", side_effect=urllib.error.URLError("no dns")):
        failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index())
    assert "PUBLISH_GATE_REPORT_URL_UNREACHABLE" in failures


def test_head_rejection_falls_back_to_get(tmp_path):
    responses = [_http_error(405), _response(200)]
    with mock.patch("sandboxer_v0.publish_gate.urllib.request.urlopen", side_effect=responses) as urlopen:
        failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index())
    assert failures == []
    assert [call.args[0].get_method() for call in urlopen.call_args_list] == ["HEAD", "GET"]


def test_require_http_false_never_touches_the_network(tmp_path):
    with mock.patch("sandboxer_v0.publish_gate.urllib.request.urlopen", side_effect=AssertionError("network disabled")):
        failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index(), require_http=False)
    assert not any(failure.startswith("PUBLISH_GATE_REPORT_URL") for failure in failures)


def test_check_report_url_http_returns_status_directly():
    with mock.patch("sandboxer_v0.publish_gate.urllib.request.urlopen", return_value=_response(200)) as urlopen:
        assert check_report_url_http(REPORT_URL) == 200
    urlopen.assert_called_once()


# --- Rule 4: the publications index must list the slug as indexed.


def test_index_entry_must_match_the_slug(tmp_path):
    other = {"publications": [{"id": "another-series"}]}
    failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, other, require_http=False)
    assert f"PUBLISH_GATE_INDEX_ENTRY_MISSING:{SLUG}" in failures


def test_explicitly_unindexed_entry_blocks(tmp_path):
    failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index(indexed=False), require_http=False)
    assert failures == [f"PUBLISH_GATE_INDEX_NOT_INDEXED:{SLUG}"]


def test_entry_listed_without_indexed_field_counts_as_indexed(tmp_path):
    failures = check_publish_gate(_write_bundle(tmp_path), REPORT_URL, _loaded_index(), require_http=False)
    assert failures == []


def test_bare_entry_list_and_publications_json_path_are_accepted(tmp_path):
    bundle = _write_bundle(tmp_path)
    assert check_publish_gate(bundle, REPORT_URL, [_index_entry()], require_http=False) == []
    publications_json = tmp_path / "data" / "publications.json"
    publications_json.parent.mkdir()
    publications_json.write_text(json.dumps(_loaded_index()), encoding="utf-8")
    assert check_publish_gate(bundle, REPORT_URL, publications_json, require_http=False) == []


def test_missing_or_unreadable_index_blocks(tmp_path):
    bundle = _write_bundle(tmp_path)
    assert "PUBLISH_GATE_INDEX_MISSING" in check_publish_gate(bundle, REPORT_URL, None, require_http=False)
    assert "PUBLISH_GATE_INDEX_MISSING" in check_publish_gate(bundle, REPORT_URL, tmp_path / "gone.json", require_http=False)
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    assert "PUBLISH_GATE_INDEX_MISSING" in check_publish_gate(bundle, REPORT_URL, broken, require_http=False)


def test_all_blocking_rules_are_collected_together(tmp_path):
    failures = check_publish_gate(None, "https://sandboxer.example/x", None, require_http=False)
    assert failures == [
        "PUBLISH_GATE_BUNDLE_MISSING",
        "PUBLISH_GATE_REPORT_URL_PLACEHOLDER:sandboxer.example",
        "PUBLISH_GATE_INDEX_MISSING",
    ]


# --- YouTube upload integration: real uploads are refused behind the gate.


def test_real_upload_without_gate_inputs_is_refused(tmp_path):
    uploader = YoutubeUploader(dry_run=False)
    with pytest.raises(PublishGateError, match="PUBLISH_GATE_INPUTS_REQUIRED"):
        uploader.upload(_video(tmp_path), title="t", description="d", approved=True)


def test_real_upload_is_refused_when_the_gate_reports_failures(tmp_path):
    uploader = YoutubeUploader(dry_run=False)
    with pytest.raises(PublishGateError, match="PUBLISH_GATE_BLOCKED.*BUNDLE_MISSING"):
        uploader.upload(
            _video(tmp_path),
            title="t",
            description="d",
            approved=True,
            bundle_path=tmp_path / "missing.json",
            report_url=REPORT_URL,
            publications_index=_loaded_index(),
            require_http=False,
        )


def test_allow_ungated_is_an_explicit_bypass_even_for_failing_evidence(tmp_path):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    result = uploader.upload(
        _video(tmp_path),
        title="t",
        description="d",
        approved=True,
        bundle_path=tmp_path / "missing.json",
        allow_ungated=True,
    )
    assert result["dry_run"] is False and result["video_id"] == "sbx-video-0001"


def test_fake_service_upload_with_partial_inputs_still_validates_them(tmp_path):
    uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
    with pytest.raises(PublishGateError):
        uploader.upload(
            _video(tmp_path),
            title="t",
            description="d",
            approved=True,
            bundle_path=tmp_path / "missing.json",
            require_http=False,
        )


def test_dry_run_rehearsal_stays_credential_free_and_ungated(tmp_path):
    result = YoutubeUploader(dry_run=True).upload(_video(tmp_path), title="t", description="d")
    assert result["dry_run"] is True and result["video_id"].startswith("sbx-dryrun-")


def test_real_upload_proceeds_once_the_gate_passes(tmp_path, monkeypatch):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(dry_run=False)
    monkeypatch.setattr(uploader, "_youtube", lambda: service)
    result = uploader.upload(
        _video(tmp_path),
        title="t",
        description="d",
        approved=True,
        bundle_path=_write_bundle(tmp_path),
        report_url=REPORT_URL,
        publications_index=_loaded_index(),
        require_http=False,
    )
    assert result["video_id"] == "sbx-video-0001"
    assert [call["method"] for call in service.calls] == ["videos.insert"]
