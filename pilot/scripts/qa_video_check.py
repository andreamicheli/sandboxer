"""QA checks for a rendered SandBoxer episode run directory.

Usage:
    python scripts/qa_video_check.py --run-dir artifacts/runs/match-full-pipeline-v7

Prints human-readable PASS/FAIL/WARN lines and a JSON verdict. Exit 0 only if
every hard check passes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration,size:stream=codec_name,width,height,r_frame_rate,pix_fmt,duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def check(name: str, ok: bool | None, reason: str = "", warn_only: bool = False) -> dict:
    if ok is True:
        status = "PASS"
    elif ok is None or warn_only:
        status = "WARN"
    else:
        status = "FAIL"
    return {"name": name, "status": status, "reason": reason}


def qa_run(run_dir: Path) -> tuple[list[dict], bool]:
    results: list[dict] = []
    video = run_dir / "video-only.mp4"

    if not video.exists():
        return [check("video_exists", False, f"{video} missing")], False

    probe = _ffprobe(video)
    streams = probe.get("streams", [])
    fmt = probe.get("format", {})
    vstreams = [s for s in streams if s.get("codec_name") == "h264"]
    astreams = [s for s in streams if s.get("codec_name") == "aac"]

    # 1. duration vs manifest
    manifest_path = run_dir / "video_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    fps = int(manifest.get("fps", 30))
    total_frames = sum(s["duration_frames"] for s in manifest["scenes"])
    expected_dur = total_frames / fps
    actual_dur = float(fmt.get("duration", 0))
    dur_ok = abs(actual_dur - expected_dur) / expected_dur <= 0.05
    results.append(check(
        "duration_within_5pct", dur_ok,
        f"expected~{expected_dur:.1f}s got {actual_dur:.2f}s",
    ))

    # 2. resolution/fps/pixfmt
    v = vstreams[0] if vstreams else {}
    res_ok = (v.get("width"), v.get("height")) == (1920, 1080)
    fps_ok = str(v.get("r_frame_rate")) == f"{fps}/1"
    pix_ok = v.get("pix_fmt", "yuv420p") == "yuv420p"  # pix_fmt may be absent from probe
    results.append(check("resolution_1920x1080", res_ok, str((v.get("width"), v.get("height")))))
    results.append(check("fps_matches_manifest", fps_ok, str(v.get("r_frame_rate"))))
    results.append(check("h264_codec", bool(vstreams), ""))
    results.append(check("pixfmt_yuv420p", pix_ok, str(v.get("pix_fmt")), warn_only=True))

    # 3. audio present, near video duration
    a = astreams[0] if astreams else {}
    a_dur = float(a.get("duration") or 0)
    audio_ok = bool(astreams) and abs(a_dur - actual_dur) <= 2.0
    results.append(check("audio_aac_near_duration", audio_ok,
                         f"audio={a_dur:.2f}s video={actual_dur:.2f}s"))

    # 4. commentary lines valid and in range
    commentary = manifest.get("commentary", [])
    bad_comm = [c for c in commentary
                if not c.get("text")
                or c["start_frame"] < 0 or c["end_frame"] > total_frames]
    results.append(check("commentary_valid_in_range", not bad_comm,
                         f"{len(bad_comm)} bad of {len(commentary)}"))

    # 5. terminal events
    terminal = manifest.get("terminal") or []
    oob = [e for e in terminal if e.get("at_frame", 0) >= total_frames]
    panes = {e.get("pane") for e in terminal}
    dup_ids = len(terminal) != len({e.get("event_id") for e in terminal})
    results.append(check("terminal_events_in_range", not oob, f"{len(oob)} out of bounds"))
    results.append(check("terminal_both_panes_active", panes >= {0, 1}, f"panes={sorted(panes)}"))
    results.append(check("terminal_no_duplicate_ids", not dup_ids, ""))

    # 6. report sanity
    report_path = run_dir / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    rep_ok = (
        bool(report.get("schema"))
        and bool(report.get("outcome"))
        and bool(report.get("technical_chapters"))
        and str(report.get("report_url", "")).startswith("https://")
    )
    results.append(check("report_complete_https_url", rep_ok, str(report.get("report_url"))))

    # 7. scene ordering
    types = [s["type"] for s in manifest["scenes"]]
    order_ok = (
        types[0] == "custom_intro"
        and types[-1] == "factual_recap"
        and all(not (a_ == b_ and a_.endswith("_card"))
                for a_, b_ in zip(types, types[1:]))
    )
    match_frames = sum(s["duration_frames"] for s in manifest["scenes"]
                       if s["type"] == "match")
    match_share = match_frames / total_frames > 0.5
    results.append(check("scene_order_intro_first_recap_last", order_ok, " -> ".join(types)))
    results.append(check("match_majority_runtime", match_share,
                         f"{match_share:.0%} ({match_frames}/{total_frames})"))

    hard_fail = any(r["status"] == "FAIL" for r in results)
    return results, not hard_fail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    results, ok = qa_run(args.run_dir)
    for r in results:
        print(f"[{r['status']}] {r['name']}: {r['reason']}")
    print(json.dumps({"ok": ok, "results": results}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
