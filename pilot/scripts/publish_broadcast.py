"""Render the narrated Series video audio and perform the YouTube handoff.

Usage (credential-free rehearsal):

    uv run python scripts/publish_broadcast.py \\
        --manifest ../artifacts/video-manifest.json \\
        --video ../artifacts/delivery.mp4 \\
        --tts fake --youtube dry-run --out ../artifacts/broadcast.json

Real mode requires control-plane credentials (GEMINI_API_KEY and the
YOUTUBE_* refresh-token env vars), an explicit human approval
(``--approved-by``), and a confirmation:

    uv run python scripts/publish_broadcast.py \\
        --manifest ../artifacts/video-manifest.json \\
        --report ../artifacts/report.json \\
        --video ../artifacts/delivery.mp4 \\
        --captions ../artifacts/captions.vtt \\
        --thumb ../artifacts/thumbnail.png \\
        --tts real --youtube real --privacy unlisted \\
        --approved-by editor --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandboxer_v0.tts import (
    DEFAULT_FISH_TTS_MODEL,
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
    model = manifest.get("tts", {}).get("expected", {}).get("model") or DEFAULT_TTS_MODEL
    if mode == "fake":
        return FakeTtsAdapter(model=model)
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
    # Free-tier quota is per model: an approved fallback chain lets an
    # exhausted primary continue on the next model, recorded via models_used.
    allow_fallback = os.environ.get("SANDBOXER_TTS_ALLOW_FALLBACK", "").strip().lower() in ("1", "true", "yes")
    fallback = os.environ.get("GEMINI_TTS_FALLBACK_MODELS", "").strip()
    fallback_models = tuple(m.strip() for m in fallback.split(",") if m.strip()) if fallback else DEFAULT_TTS_FALLBACK_MODELS
    return GeminiTtsAdapter(model=model, fallback_models=fallback_models, allow_fallback=allow_fallback)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--captions", type=Path, default=None)
    parser.add_argument("--thumb", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("broadcast-record.json"))
    parser.add_argument("--audio-dir", type=Path, default=None)
    parser.add_argument("--tts", choices=("real", "fake", "fish"), default="real")
    parser.add_argument("--youtube", choices=("real", "fake", "dry-run"), default="dry-run")
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="unlisted")
    parser.add_argument("--playlist", default=None)
    parser.add_argument("--approved-by", default=None)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    manifest = _load(args.manifest, "manifest")
    if manifest.get("schema") != "sandboxer.video-manifest.v1":
        raise SystemExit("MANIFEST_SCHEMA_UNSUPPORTED")
    report = _load(args.report, "report")
    if not args.video.is_file() or args.video.stat().st_size == 0:
        raise SystemExit("VIDEO_MISSING")
    audio_dir = args.audio_dir or args.out.with_suffix(".audio")

    # 1. TTS commentary blocks (bounded, hashed; drift fails preflight).
    try:
        adapter = _tts_adapter(args.tts, manifest)
        tts_section = render_commentary_audio(manifest, adapter, out_dir=audio_dir)
    except (TtsError, ValueError) as error:
        raise SystemExit(f"TTS_FAILED: {error}") from error

    # 2. YouTube handoff (metadata, resumable upload, captions, thumbnail).
    try:
        metadata = youtube_metadata(manifest, report)
        rehearsal = args.youtube in ("fake", "dry-run")
        if args.youtube == "fake":
            uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
        elif args.youtube == "dry-run":
            uploader = YoutubeUploader(dry_run=True)
        else:
            if not args.approved_by:
                raise SystemExit("APPROVAL_REQUIRED: pass --approved-by <reviewer> for a real upload")
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

    record = {
        "schema": "sandboxer.broadcast.v1",
        "manifest_hash": manifest.get("manifest_hash"),
        "source_bundle_hash": manifest.get("source_bundle_hash"),
        "tts": {
            "model": tts_section["expected"]["model"],
            "models_used": tts_section.get("models_used", [tts_section["expected"]["model"]]),
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
