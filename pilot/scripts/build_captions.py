"""Generate artifacts/captions.vtt from a video manifest's commentary schedule."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent / "artifacts"


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def main() -> int:
    manifest = json.loads((ROOT / "video-manifest.json").read_text(encoding="utf-8"))
    fps = manifest["fps"]
    lines: list[str] = ["WEBVTT", ""]
    for index, line in enumerate(manifest.get("commentary", ())):
        start = line["start_frame"] / fps
        end = line["end_frame"] / fps
        speaker = line.get("voice_role", "play_by_play")
        lines.append(f"{index + 1:02d}")
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.append(f"<v {speaker}>{line['text']}")
        lines.append("")
    (ROOT / "captions.vtt").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {ROOT / 'captions.vtt'} ({len(manifest.get('commentary', ()))} cues)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
