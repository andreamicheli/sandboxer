"""YouTube publication handoff for the broadcast layer.

The uploader talks to the YouTube Data API v3 with an OAuth refresh token
stored in the control plane (never in a Runner).  Uploads default to
``unlisted`` so a human can review before the episode becomes public; every
real upload requires explicit approval.  ``dry_run`` and the fake service keep
the credential-free rehearsal path deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_CATEGORY_ID = "28"  # Science & Technology
DEFAULT_PRIVACY_STATUS = "unlisted"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class YoutubeError(ValueError):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class YoutubeCredentials:
    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str = TOKEN_URI

    @classmethod
    def from_env(cls) -> "YoutubeCredentials":
        values = (
            os.environ.get("YOUTUBE_CLIENT_ID"),
            os.environ.get("YOUTUBE_CLIENT_SECRET"),
            os.environ.get("YOUTUBE_REFRESH_TOKEN"),
        )
        if not all(values):
            raise YoutubeError("YOUTUBE_CREDENTIAL_MISSING")
        return cls(client_id=values[0], client_secret=values[1], refresh_token=values[2])

    def to_google_credentials(self) -> Any:
        from google.oauth2.credentials import Credentials  # lazy import

        return Credentials(
            token=None,
            refresh_token=self.refresh_token,
            token_uri=self.token_uri,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )


def _seconds(frames: int, fps: int) -> int:
    return round(frames / fps) if fps else 0


def _timecode(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def youtube_metadata(
    manifest: Mapping[str, Any],
    report: Mapping[str, Any] | None = None,
    *,
    video_url: str | None = None,
    report_url: str | None = None,
    publish_at: str | None = None,
    category_id: str = DEFAULT_CATEGORY_ID,
) -> dict[str, Any]:
    """Derive upload metadata (title, description, tags, status) from the frozen manifest.

    ``report_url`` is the canonical report location on the site (linked from the
    description); it overrides ``report["report_url"]``.  ``publish_at``, when
    given, is an ISO date announcing when the episode becomes the indexed,
    featured result on the site — the upload itself stays unlisted for preview.
    """
    fps = int(manifest.get("fps", 30))
    identities = tuple(str(item) for item in manifest.get("identities", ()))
    report = report or {}
    winner = report.get("outcome", {}).get("winner") if isinstance(report.get("outcome"), Mapping) else None
    report_url = report_url or report.get("report_url")
    title = str(report.get("title") or f"Sandboxer simulated CTF: {' vs '.join(identities)}")
    title = " ".join(title.split())[:100]
    lines = [
        "Sandboxer is an experimental benchmark in a simulated capture-the-flag Arena with synthetic-only targets.",
        "This episode is a Showcase Evaluation, not a scientific benchmark, and establishes no global ranking.",
    ]
    if winner:
        lines.append(f"Series winner: {winner}.")
    chapter_lines = ["Chapters:"]
    cursor = 0
    for scene in manifest.get("scenes", ()):
        kind = scene.get("type")
        label = {
            "cold_open": "Cold open",
            "model_cards_and_rules": "Models and rules",
            "match": f"Match {scene.get('match_number', '?')}",
            "intermission": "Intermission",
            "factual_recap": "Recap and report",
        }.get(kind, kind)
        chapter_lines.append(f"{_timecode(_seconds(cursor, fps))} {label}")
        cursor += int(scene.get("duration_frames", 0))
    lines.append(" ".join(chapter_lines))
    lines.append("Commentary lines and visual beats are traceable to timestamped Match Telemetry event IDs.")
    if report_url:
        lines.append(f"Canonical Series Result Report: {report_url}")
    if publish_at:
        lines.append(f"This result becomes the featured, indexed Match on the Sandboxer site on {publish_at}.")
    if video_url:
        lines.append(f"Source bundle and evidence: {video_url}")
    description = "\n".join(lines)[:5000]
    tags = ["sandboxer", "AI evaluation", "simulated CTF", "cybersecurity", "LLM", "model comparison"]
    tags += [item for item in identities if item]
    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": DEFAULT_PRIVACY_STATUS,
            "madeForKids": False,
            "selfDeclaredMadeForKids": False,
        },
    }


class _FakeRequest:
    def __init__(self, service: "FakeYoutubeService", method: str, **kwargs: Any) -> None:
        self._service = service
        self._method = method
        self._kwargs = kwargs

    def execute(self) -> dict[str, Any]:
        return self._service._respond(self._method, self._kwargs)

    def next_chunk(self) -> tuple[None, dict[str, Any]]:
        return None, self.execute()


class _FakeMethod:
    def __init__(self, service: "FakeYoutubeService", resource: str) -> None:
        self._service = service
        self._resource = resource

    def list(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest(self._service, f"{self._resource}.list", **kwargs)

    def insert(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest(self._service, f"{self._resource}.insert", **kwargs)

    def set(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest(self._service, f"{self._resource}.set", **kwargs)


class FakeYoutubeService:
    """Deterministic in-memory YouTube service for tests and fake rehearsal."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def channels(self) -> _FakeMethod:
        return _FakeMethod(self, "channels")

    def videos(self) -> _FakeMethod:
        return _FakeMethod(self, "videos")

    def thumbnails(self) -> _FakeMethod:
        return _FakeMethod(self, "thumbnails")

    def captions(self) -> _FakeMethod:
        return _FakeMethod(self, "captions")

    def playlistItems(self) -> _FakeMethod:
        return _FakeMethod(self, "playlistItems")

    def _respond(self, method: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        record = {"method": method, "args": {key: value for key, value in kwargs.items() if key not in ("media_body",)}}
        self.calls.append(record)
        if method == "channels.list":
            return {"items": [{"id": "sbx-channel", "snippet": {"title": "Sandboxer"}}]}
        if method == "videos.insert":
            self._counter += 1
            body = kwargs.get("body") or {}
            status = body.get("status") or {}
            return {"id": f"sbx-video-{self._counter:04d}", "status": {"privacyStatus": status.get("privacyStatus", "unlisted")}}
        if method == "thumbnails.set":
            return {"items": [{"url": f"https://i.ytimg.com/vi/{kwargs.get('videoId')}/hqdefault.jpg"}]}
        if method == "captions.insert":
            snippet = (kwargs.get("body") or {}).get("snippet") or {}
            return {"id": f"sbx-caption-{self._counter:04d}", "snippet": snippet}
        if method == "playlistItems.insert":
            return {"id": f"sbx-playlist-item-{self._counter:04d}"}
        raise YoutubeError(f"YOUTUBE_FAKE_UNSUPPORTED: {method}")


class YoutubeUploader:
    """Resumable YouTube upload behind an injectable service and approval gate."""

    def __init__(
        self,
        *,
        service: Any | None = None,
        credentials: YoutubeCredentials | None = None,
        dry_run: bool = False,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        self._service = service
        self._credentials = credentials
        self.dry_run = dry_run
        self._chunk_size = chunk_size
        self.calls: list[dict[str, Any]] = []

    def _youtube(self) -> Any:
        if self._service is not None:
            return self._service
        if self.dry_run:
            return FakeYoutubeService()
        if self._credentials is None:
            self._credentials = YoutubeCredentials.from_env()
        from googleapiclient.discovery import build  # lazy import

        return build("youtube", "v3", credentials=self._credentials.to_google_credentials(), cache_discovery=False)

    @staticmethod
    def _require_file(path: Path | str, label: str) -> Path:
        resolved = Path(path)
        if not resolved.is_file() or resolved.stat().st_size == 0:
            raise YoutubeError(f"YOUTUBE_{label}_MISSING")
        return resolved

    def preflight(self, *, video_path: Path | str) -> dict[str, Any]:
        video = self._require_file(video_path, "VIDEO")
        if self.dry_run:
            return {"dry_run": True, "video": str(video), "channel": {"id": "sbx-channel", "title": "Sandboxer (dry run)"}}
        youtube = self._youtube()
        try:
            channels = youtube.channels().list(part="snippet", mine=True).execute()
        except Exception as error:
            raise YoutubeError("YOUTUBE_PREFLIGHT_FAILED") from error
        items = channels.get("items") or []
        if not items:
            raise YoutubeError("YOUTUBE_CHANNEL_UNREACHABLE")
        channel = items[0]
        return {
            "dry_run": False,
            "video": str(video),
            "channel": {"id": channel.get("id"), "title": (channel.get("snippet") or {}).get("title")},
        }

    def _fake_video_id(self, video_path: Path) -> str:
        digest = hashlib.sha256(video_path.read_bytes()).hexdigest()[:10]
        return f"sbx-dryrun-{digest}"

    def upload(
        self,
        video_path: Path | str,
        *,
        title: str,
        description: str,
        tags: Sequence[str] = (),
        category_id: str = DEFAULT_CATEGORY_ID,
        privacy_status: str = DEFAULT_PRIVACY_STATUS,
        made_for_kids: bool = False,
        publish_at: str | None = None,
        approved: bool = False,
    ) -> dict[str, Any]:
        video = self._require_file(video_path, "VIDEO")
        if privacy_status not in {"private", "unlisted", "public"}:
            raise YoutubeError("YOUTUBE_PRIVACY_INVALID")
        if not title.strip() or not description.strip():
            raise YoutubeError("YOUTUBE_METADATA_INVALID")
        if not self.dry_run and not approved:
            raise YoutubeError("YOUTUBE_NOT_APPROVED")
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": list(tags),
                "categoryId": category_id,
                "defaultLanguage": "en",
                "defaultAudioLanguage": "en",
            },
            "status": {
                "privacyStatus": privacy_status,
                "madeForKids": made_for_kids,
                "selfDeclaredMadeForKids": made_for_kids,
                **({"publishAt": publish_at} if publish_at else {}),
            },
        }
        if self.dry_run:
            video_id = self._fake_video_id(video)
            self.calls.append({"method": "videos.insert", "video_id": video_id, "privacy_status": privacy_status, "approved": approved})
            return {"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "privacy_status": privacy_status, "dry_run": True}
        youtube = self._youtube()
        try:
            from googleapiclient.http import MediaFileUpload  # lazy import

            media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=self._chunk_size)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response: dict[str, Any] | None = None
            while response is None:
                _status, response = request.next_chunk()
        except Exception as error:
            raise YoutubeError("YOUTUBE_UPLOAD_FAILED") from error
        video_id = response.get("id")
        if not video_id:
            raise YoutubeError("YOUTUBE_UPLOAD_FAILED")
        self.calls.append({"method": "videos.insert", "video_id": video_id, "privacy_status": privacy_status, "approved": True})
        return {"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "privacy_status": privacy_status, "dry_run": False}

    def set_thumbnail(self, video_id: str, thumbnail_path: Path | str) -> dict[str, Any]:
        thumbnail = self._require_file(thumbnail_path, "THUMBNAIL")
        if not video_id:
            raise YoutubeError("YOUTUBE_VIDEO_ID_MISSING")
        if self.dry_run:
            self.calls.append({"method": "thumbnails.set", "video_id": video_id})
            return {"video_id": video_id, "url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", "dry_run": True}
        youtube = self._youtube()
        try:
            from googleapiclient.http import MediaFileUpload  # lazy import

            request = youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail), mimetype="image/png"))
            response = request.execute()
        except Exception as error:
            raise YoutubeError(f"YOUTUBE_THUMBNAIL_FAILED: {error}") from error
        items = response.get("items") or []
        url = (items[0].get("url") if items else None) or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        self.calls.append({"method": "thumbnails.set", "video_id": video_id})
        return {"video_id": video_id, "url": url, "dry_run": False}

    def upload_captions(
        self,
        video_id: str,
        caption_path: Path | str,
        *,
        language: str = "en",
        is_draft: bool = True,
        sync: bool = True,
    ) -> dict[str, Any]:
        captions = self._require_file(caption_path, "CAPTIONS")
        if not video_id:
            raise YoutubeError("YOUTUBE_VIDEO_ID_MISSING")
        # ``name`` is required by the captions.insert endpoint (empty string is
        # valid); omitting it returns a 400 invalidMetadata.
        body = {"snippet": {"videoId": video_id, "language": language, "name": "", "isDraft": is_draft}}
        if self.dry_run:
            self.calls.append({"method": "captions.insert", "video_id": video_id, "language": language, "is_draft": is_draft})
            return {"video_id": video_id, "caption_id": f"sbx-caption-{video_id}", "is_draft": is_draft, "dry_run": True}
        youtube = self._youtube()
        try:
            from googleapiclient.http import MediaFileUpload  # lazy import

            request = youtube.captions().insert(
                part="snippet",
                body=body,
                media_body=MediaFileUpload(str(captions), mimetype="text/vtt"),
                sync=sync,
            )
            response = request.execute()
        except Exception as error:
            raise YoutubeError(f"YOUTUBE_CAPTIONS_FAILED: {error}") from error
        self.calls.append({"method": "captions.insert", "video_id": video_id, "language": language, "is_draft": is_draft})
        return {"video_id": video_id, "caption_id": response.get("id"), "is_draft": is_draft, "dry_run": False}

    def add_to_playlist(self, playlist_id: str, video_id: str) -> dict[str, Any]:
        if not playlist_id or not video_id:
            raise YoutubeError("YOUTUBE_PLAYLIST_INVALID")
        if self.dry_run:
            self.calls.append({"method": "playlistItems.insert", "playlist_id": playlist_id, "video_id": video_id})
            return {"playlist_id": playlist_id, "video_id": video_id, "dry_run": True}
        youtube = self._youtube()
        try:
            response = youtube.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
        except Exception as error:
            raise YoutubeError("YOUTUBE_PLAYLIST_FAILED") from error
        self.calls.append({"method": "playlistItems.insert", "playlist_id": playlist_id, "video_id": video_id})
        return {"playlist_id": playlist_id, "video_id": video_id, "item_id": response.get("id"), "dry_run": False}
