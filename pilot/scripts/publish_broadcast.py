"""Render the narrated Series video audio and perform the YouTube handoff.

Usage (credential-free rehearsal):

    uv run python scripts/publish_broadcast.py \\
        --manifest ../artifacts/video-manifest.json \\
        --video ../artifacts/delivery.mp4 \\
        --tts fake --youtube dry-run --out ../artifacts/broadcast.json

Real mode requires control-plane credentials (GEMINI_API_KEY and the
YOUTUBE_* refresh-token env vars).  Publishing is unattended by default
(human gates off): the upload stays ``unlisted`` for preview and the site
indexes the result on the next odd day.  Pass ``--manual`` to re-enable the
human gate (``--approved-by`` plus an interactive confirmation).

    uv run python scripts/publish_broadcast.py \\
        --manifest ../artifacts/video-manifest.json \\
        --report ../artifacts/report.json \\
        --video ../artifacts/delivery.mp4 \\
        --captions ../artifacts/captions.vtt \\
        --thumb ../artifacts/thumbnail.png \\
        --tts real --youtube real --privacy unlisted \\
        --approved-by editor --yes

Manual (gated) publishing: ``--manual`` re-enables the reviewer requirement
(``--approved-by``) and the interactive confirmation.  ``--publish-at``
(default: the next odd day) always schedules when the site indexes the result
as featured while the upload stays unlisted for preview:

    uv run python scripts/publish_broadcast.py \\
        --manifest ../artifacts/video-manifest.json \\
        --report ../artifacts/report.json \\
        --video ../artifacts/delivery.mp4 \\
        --tts fish --youtube real --privacy unlisted \\
        --bundle evidence.json --site-base-url https://sandboxer.example
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandboxer_v0.publication_index import PublicationIndexError, upsert_publication
from sandboxer_v0.report import ResultReportError, build_result_report
from sandboxer_v0.report_latex import build_report_latex, compile_latex, render_narrative_latex
from sandboxer_v0.report_narrative import (
    DeterministicReportNarrativeDrafter,
    HeadlessReportNarrativeDrafter,
    draft_report_narrative,
    render_narrative_html,
)
from sandboxer_v0.schedule import first_publication_date, parse_iso_date
from sandboxer_v0.tts import (
    DEFAULT_FISH_TTS_MODEL,
    DEFAULT_GEMINI_TTS_MODEL,
    DEFAULT_TTS_FALLBACK_MODELS,
    DEFAULT_TTS_MODEL,
    FakeTtsAdapter,
    FishAudioTtsAdapter,
    GeminiTtsAdapter,
    TtsError,
    parse_voice_spec,
    render_commentary_audio,
)
from sandboxer_v0.youtube import FakeYoutubeService, YoutubeError, YoutubeUploader, youtube_metadata
from sandboxer_v0.video import validate_provenance


def _confirm(action: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty() or input(f"Confirm {action} [type YES]: ") != "YES":
        raise SystemExit("CONFIRMATION_REQUIRED")


def _best_effort(action):
    """Run a best-effort enhancement; record the failure instead of aborting.

    A thumbnail or captions hiccup (e.g. an unverified channel that cannot set
    custom thumbnails) must not lose the uploaded video or the broadcast
    record.
    """
    try:
        return action()
    except YoutubeError as error:
        return {"error": str(error)}


def _load(path: Path | None, label: str) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{label} unreadable: {path}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object: {path}")
    return value


def _tts_adapter(mode: str, manifest: Mapping[str, Any]) -> Any:
    expected_model = str(manifest.get("tts", {}).get("expected", {}).get("model") or "")
    pinned_provider = str(manifest.get("tts", {}).get("requested", {}).get("provider") or "")
    if mode == "fake":
        return FakeTtsAdapter(model=expected_model or DEFAULT_TTS_MODEL)
    if mode == "fish":
        fish_key = os.environ.get("FISH_API_KEY")
        if not fish_key:
            raise SystemExit("FISH_CREDENTIAL_MISSING: set FISH_API_KEY")
        reference_ids = parse_voice_spec(os.environ.get("FISH_TTS_VOICES", ""))
        if not reference_ids:
            raise SystemExit('FISH_VOICES_MISSING: set FISH_TTS_VOICES="Kore=<ref>,Charon=<ref>"')
        return FishAudioTtsAdapter(
            api_key=fish_key,
            model=os.environ.get("FISH_TTS_MODEL") or DEFAULT_FISH_TTS_MODEL,
            reference_ids=reference_ids,
        )
    if mode != "real":
        raise SystemExit("TTS mode must be 'real', 'fake', or 'fish'")
    # Explicit Gemini override: honor a Gemini model only when the manifest
    # itself was built with an explicit Gemini pin.
    model = expected_model if expected_model.startswith("gemini") else DEFAULT_GEMINI_TTS_MODEL
    # Free-tier quota is per model: an approved fallback chain lets an
    # exhausted primary continue on the next model, recorded via models_used.
    allow_fallback = os.environ.get("SANDBOXER_TTS_ALLOW_FALLBACK", "").strip().lower() in ("1", "true", "yes")
    fallback = os.environ.get("GEMINI_TTS_FALLBACK_MODELS", "").strip()
    fallback_models = tuple(m.strip() for m in fallback.split(",") if m.strip()) if fallback else DEFAULT_TTS_FALLBACK_MODELS
    # A Gemini run against a manifest pinned to another provider (the default
    # is Fish) is a deliberate substitution: approve it so the drift is
    # recorded, never silent (and never a hard preflight failure).
    allow_fallback = allow_fallback or (bool(pinned_provider) and pinned_provider != "gemini")
    return GeminiTtsAdapter(model=model, fallback_models=fallback_models, allow_fallback=allow_fallback)


def _slugify(value: str) -> str:
    """Normalize a series id into a site-safe slug (lowercase, alnum + dash)."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80]


def _series_slug(bundle: Mapping[str, Any]) -> str:
    """A stable, safe report slug for a bundle, derived from its series id."""
    specification = bundle.get("public") if isinstance(bundle.get("public"), Mapping) else None
    specification = specification.get("specification") if isinstance(specification, Mapping) else None
    series_id = specification.get("series_id") if isinstance(specification, Mapping) else None
    slug = _slugify(str(series_id)) if series_id else ""
    return slug or f"series-{str(bundle.get('bundle_hash', ''))[:12]}"


def _draft_narrative(model: Mapping[str, Any]) -> dict[str, Any]:
    """Draft the report narrative with a text model, falling back deterministically."""
    try:
        return draft_report_narrative(model, drafter=HeadlessReportNarrativeDrafter())
    except Exception as error:  # an ungrounded/failed draft must never block publication
        print(f"report narrative LLM draft failed ({error}); using deterministic fallback", file=sys.stderr)
        return draft_report_narrative(model, drafter=DeterministicReportNarrativeDrafter())


def _build_site_report(
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    publish_at: str | None,
) -> dict[str, Any]:
    """Generate and stage the canonical, detailed, and LLM-narrative reports.

    Returns the canonical ``report_url`` (the LLM narrative subpage), the
    ``slug``, a ``publication`` entry for ``publications.json`` (its
    ``video_url`` is filled after the upload), and a ``broadcast_report`` used
    for the YouTube title/winner metadata.  With no ``--bundle`` this is a
    no-op preserving the legacy ``--report-url`` behaviour (the site report is
    then expected to be published elsewhere).
    """
    if args.bundle is None:
        return {"report_url": args.report_url, "slug": None, "publication": None, "broadcast_report": None}
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    try:
        report = build_result_report(bundle)
        latex = build_report_latex(bundle, compile_pdf=True)
    except (ResultReportError, RuntimeError) as error:
        raise SystemExit(f"REPORT_FAILED: {error}") from error
    model = report.model.to_dict()
    winner = str(model["outcome"]["winner"])
    competitors = [str(item["public_name"]) for item in model["competitor_manifests"]]
    title = f"Sandboxer Series: {winner} wins ({' vs '.join(competitors)})"
    slug = args.report_slug or _series_slug(bundle)
    if not slug:
        raise SystemExit("REPORT_SLUG_MISSING")
    base_url = (args.site_base_url or "").rstrip("/")
    if not base_url:
        raise SystemExit("SITE_BASE_URL_REQUIRED: pass --site-base-url (or set SANDBOXER_SITE_BASE_URL) with --bundle")
    reports_dir = args.site_root / "assets" / "reports" / slug
    charts_dir = reports_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "report.html").write_text(report.html, encoding="utf-8")
    (reports_dir / "report.json").write_text(report.json, encoding="utf-8")
    (reports_dir / "report.pdf").write_bytes(report.pdf)
    (reports_dir / "report-detailed.tex").write_text(latex.tex, encoding="utf-8")
    (reports_dir / "report-detailed.pdf").write_bytes(latex.pdf)
    for name, content in latex.charts.items():
        (charts_dir / name).write_bytes(content)
    (reports_dir / "evidence.json").write_bytes(args.bundle.read_bytes())
    narrative = _draft_narrative(model)
    narrative_tex = render_narrative_latex(model, narrative, list(latex.charts.keys()))
    try:
        narrative_pdf = compile_latex(narrative_tex, latex.charts)
    except RuntimeError as error:
        raise SystemExit(f"REPORT_FAILED: {error}") from error
    (reports_dir / "narrative.html").write_text(render_narrative_html(model, narrative), encoding="utf-8")
    (reports_dir / "narrative.tex").write_text(narrative_tex, encoding="utf-8")
    (reports_dir / "narrative.pdf").write_bytes(narrative_pdf)
    report_url = f"{base_url}/assets/reports/{slug}/narrative.html"
    publication = {
        "id": slug,
        "title": title,
        "models": [f"{item['public_name']} ({item['model_id']})" for item in model["competitor_manifests"]],
        "winner": winner,
        "report_url": report_url,
        "video_url": None,
        "publish_at": publish_at,
        "banner": f"New Match result published: {title}.",
    }
    broadcast_report = {"title": title, "outcome": {"winner": winner}, "report_url": report_url}
    return {"report_url": report_url, "slug": slug, "publication": publication, "broadcast_report": broadcast_report}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--captions", type=Path, default=None)
    parser.add_argument("--thumb", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("broadcast-record.json"))
    parser.add_argument("--audio-dir", type=Path, default=None)
    parser.add_argument("--tts", choices=("real", "fake", "fish"), default="fish",
                        help="TTS provider (default: fish; 'real' is the explicit Gemini override)")
    parser.add_argument("--youtube", choices=("real", "fake", "dry-run"), default="dry-run")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="unlisted")
    parser.add_argument("--playlist", default=None)
    parser.add_argument("--approved-by", default=None)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--manual", action="store_true",
                        help="re-enable the human approval gate (--approved-by + confirmation); default is unattended auto-approve")
    parser.add_argument("--report-url", default=None,
                        help="canonical report URL on the site, linked from the video description")
    parser.add_argument("--publish-at", default=None,
                        help="ISO date (YYYY-MM-DD) the site indexes this result as featured; default: next odd day")
    parser.add_argument("--bundle", type=Path, default=None,
                        help="frozen evidence bundle JSON; stages the canonical, detailed, and LLM-narrative reports on the site")
    parser.add_argument("--site-root", type=Path, default=Path(__file__).resolve().parents[2] / "site",
                        help="static site directory receiving assets/reports/ and data/publications.json")
    parser.add_argument("--site-base-url", default=os.environ.get("SANDBOXER_SITE_BASE_URL"),
                        help="deployed site base URL (e.g. https://sandboxer.vercel.app); required with --bundle")
    parser.add_argument("--report-slug", default=None,
                        help="report slug on the site (default: derived from the bundle series_id)")
    args = parser.parse_args(argv)

    manifest = _load(args.manifest, "manifest")
    if manifest.get("schema") != "sandboxer.video-manifest.v1":
        raise SystemExit("MANIFEST_SCHEMA_UNSUPPORTED")
    report = _load(args.report, "report")
    if not args.video.is_file() or args.video.stat().st_size == 0:
        raise SystemExit("VIDEO_MISSING")
    audio_dir = args.audio_dir or args.out.with_suffix(".audio")

    # Schedule: default to the next odd day; an explicit date must be valid.
    publish_at: str | None = None
    if args.publish_at:
        publish_at = parse_iso_date(args.publish_at).isoformat()
    elif not args.manual:
        publish_at = first_publication_date(date.today()).isoformat()

    # 1. TTS commentary blocks (bounded, hashed; drift fails preflight).
    try:
        adapter = _tts_adapter(args.tts, manifest)
        tts_section = render_commentary_audio(manifest, adapter, out_dir=audio_dir)
    except (TtsError, ValueError) as error:
        raise SystemExit(f"TTS_FAILED: {error}") from error
    # The broadcast record must identify the provider that actually produced
    # the audio: requested (pre-synthesis config) and observed (call-time) are
    # both recorded, and an inconsistent pair fails closed before upload.
    tts_provenance = {
        "requested": tts_section.get("requested"),
        "observed": tts_section.get("observed"),
        "tts_provider_drift": tts_section.get("tts_provider_drift", False),
    }
    problems = validate_provenance({"tts": {**manifest.get("tts", {}), **tts_provenance}})
    if problems:
        raise SystemExit(f"TTS_PROVENANCE_INVALID: {', '.join(problems)}")

    # 2. Site report (canonical + detailed LaTeX) staged from the frozen bundle.
    site_report = _build_site_report(args, manifest, publish_at)
    if site_report["slug"]:
        print(f"staged site report: {site_report['report_url']}", file=sys.stderr)

    # 3. YouTube handoff (metadata, resumable upload, captions, thumbnail).
    try:
        metadata = youtube_metadata(
            manifest,
            site_report["broadcast_report"] or report,
            report_url=site_report["report_url"],
            publish_at=publish_at,
        )
        rehearsal = args.youtube in ("fake", "dry-run")
        if args.youtube == "fake":
            uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
        elif args.youtube == "dry-run":
            uploader = YoutubeUploader(dry_run=True)
        else:
            if not args.manual:
                args.approved_by = args.approved_by or "auto"
            elif not args.approved_by:
                raise SystemExit("APPROVAL_REQUIRED: pass --approved-by <reviewer> for a manual upload")
            if args.manual:
                _confirm(f"YouTube upload ({args.privacy})", args.yes)
            uploader = YoutubeUploader(dry_run=False)
        preflight = uploader.preflight(video_path=args.video)
        uploaded = uploader.upload(
            args.video,
            title=metadata["snippet"]["title"],
            description=metadata["snippet"]["description"],
            tags=metadata["snippet"]["tags"],
            category_id=metadata["snippet"]["categoryId"],
            privacy_status=args.privacy,
            approved=args.approved_by is not None or rehearsal,
        )
        thumbnail = _best_effort(lambda: uploader.set_thumbnail(uploaded["video_id"], args.thumb)) if args.thumb else None
        captions = _best_effort(lambda: uploader.upload_captions(uploaded["video_id"], args.captions)) if args.captions else None
        playlist = _best_effort(lambda: uploader.add_to_playlist(args.playlist, uploaded["video_id"])) if args.playlist else None
    except YoutubeError as error:
        raise SystemExit(f"YOUTUBE_FAILED: {error}") from error

    # 4. Index the result on the site (revealed only once publish_at arrives).
    publication_entry = site_report["publication"]
    if publication_entry is not None:
        publication_entry = dict(publication_entry)
        publication_entry["video_url"] = uploaded["url"]
        try:
            upsert_publication(args.site_root / "data" / "publications.json", publication_entry)
        except PublicationIndexError as error:
            raise SystemExit(f"PUBLICATION_INDEX_FAILED: {error}") from error
        print(f"indexed site publication: {publication_entry['id']} (publish_at={publication_entry.get('publish_at')})", file=sys.stderr)

    record = {
        "schema": "sandboxer.broadcast.v1",
        "manifest_hash": manifest.get("manifest_hash"),
        "source_bundle_hash": manifest.get("source_bundle_hash"),
        "tts": {
            "model": tts_section["expected"]["model"],
            "models_used": tts_section.get("models_used", [tts_section["expected"]["model"]]),
            "requested": tts_provenance["requested"],
            "observed": tts_provenance["observed"],
            "tts_provider_drift": tts_provenance["tts_provider_drift"],
            "voices": tts_section["voices"],
            "block_count": tts_section["block_count"],
            "blocks_hash": tts_section["blocks_hash"],
            "out_dir": tts_section["out_dir"],
        },
        "youtube": {
            "video_id": uploaded["video_id"],
            "url": uploaded["url"],
            "privacy_status": uploaded["privacy_status"],
            "dry_run": uploaded["dry_run"],
            "approved_by": args.approved_by,
            "channel": preflight.get("channel"),
        },
        "publication": {
            "report_url": site_report["report_url"],
            "publish_at": publish_at,
            "slug": site_report["slug"],
            "indexed": publication_entry is not None,
        },
        "thumbnail": thumbnail,
        "captions": captions,
        "playlist": playlist,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, sort_keys=True, separators=(",", ":"), indent=2), encoding="utf-8")
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
