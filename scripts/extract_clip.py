#!/usr/bin/env python3
"""Cut a single clip out of a source file, optionally tidied and captioned.

Most of the time a clip belongs in an EDL rather than on disk by itself. This
is for the other times: auditioning a quote before committing to it, handing a
section to someone else, or building a library of pre-cut sound bites.

When the source is in the library, --snap moves the in and out points to the
nearest sentence boundary in the transcript, so the clip does not open or
close mid-word.

Usage:
  extract_clip.py interview.mp4 --in 412.3 --out 419.8 -o clips/warranty.mp4
  extract_clip.py interview.mp4 --in 6:52 --out 7:00 -o clips/warranty.mp4 --snap
  extract_clip.py interview.mp4 --in 412 --out 420 -o clips/w.mp4 --canvas 9x16 --srt
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import (  # noqa: E402
    ToolError, canvas_size, ffmpeg, fmt_clock, fmt_timecode, loudness,
    parse_time, probe,
)

SNAP_TOLERANCE = 1.5  # only move a cut point this far to reach a sentence edge


def find_in_library(src: str, library: str | None) -> tuple[str, str] | None:
    roots = [library, os.environ.get("PRODUCT_VIDEO_LIBRARY"), "library",
             os.path.join(os.path.dirname(os.path.abspath(src)), "..", "library")]
    for root in roots:
        if not root:
            continue
        root = os.path.abspath(os.path.expanduser(root))
        catalog = os.path.join(root, "catalog.json")
        if not os.path.exists(catalog):
            continue
        with open(catalog) as fh:
            data = json.load(fh)
        for slug, meta in data.get("sources", {}).items():
            if os.path.realpath(meta["media"]) == os.path.realpath(src):
                return slug, root
    return None


def load_segments(root: str, slug: str) -> list[dict]:
    with open(os.path.join(root, "transcripts", f"{slug}.words.json")) as fh:
        return json.load(fh)["segments"]


def snap(segments: list[dict], start: float, end: float) -> tuple[float, float]:
    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    best_start = min(starts, key=lambda v: abs(v - start))
    best_end = min(ends, key=lambda v: abs(v - end))
    if abs(best_start - start) <= SNAP_TOLERANCE:
        start = best_start
    if abs(best_end - end) <= SNAP_TOLERANCE:
        end = best_end
    return start, end


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--in", dest="start", required=True, help="in point (seconds or mm:ss)")
    ap.add_argument("--out", dest="end", required=True, help="out point (seconds or mm:ss)")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--snap", action="store_true", help="snap to sentence boundaries from the transcript")
    ap.add_argument("--pad", type=float, default=0.0, help="extra seconds either side")
    ap.add_argument("--canvas", help="reframe to 9x16 / 1x1 / 16x9 instead of keeping the source shape")
    ap.add_argument("--fit", default="auto", choices=["auto", "cover", "contain", "blur"])
    ap.add_argument("--normalize", action="store_true", help="bring the audio to -16 LUFS")
    ap.add_argument("--srt", action="store_true", help="also write a .srt for the clip")
    ap.add_argument("--library")
    args = ap.parse_args()

    src = os.path.abspath(os.path.expanduser(args.source))
    if not os.path.exists(src):
        print(f"error: not found: {src}", file=sys.stderr)
        return 1
    info = probe(src)
    start, end = parse_time(args.start), parse_time(args.end)

    entry = find_in_library(src, args.library)
    segments = load_segments(entry[1], entry[0]) if entry else []

    if args.snap:
        if not segments:
            print("warning: --snap needs a library transcript for this source; cutting at the given times",
                  file=sys.stderr)
        else:
            before = (start, end)
            start, end = snap(segments, start, end)
            if (start, end) != before:
                print(f"snapped {before[0]:.2f}-{before[1]:.2f} -> {start:.2f}-{end:.2f}")

    start = max(0.0, start - args.pad)
    end = min(info.duration or end + args.pad, end + args.pad)
    if end <= start:
        raise ToolError(f"out ({end}) must be after in ({start})")
    duration = end - start

    output = os.path.abspath(os.path.expanduser(args.output))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    vf = []
    if args.canvas:
        w, h = canvas_size(args.canvas)
        fit = args.fit
        if fit == "auto":
            ratio = (info.aspect / (w / h)) if info.aspect else 1.0
            fit = "cover" if 0.8 <= ratio <= 1.25 else "blur"
        if fit == "cover":
            vf.append(f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1")
        elif fit == "contain":
            vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
        else:
            vf.append(
                f"split=2[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},gblur=sigma=28,eq=brightness=-0.12[bgb];"
                f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
            )

    af = []
    if args.normalize and info.has_audio:
        af.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    cmd = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src]
    if vf:
        cmd += ["-filter_complex", "[0:v]" + ",".join(vf) + ",format=yuv420p[v]", "-map", "[v]"]
        if info.has_audio:
            cmd += ["-map", "0:a:0"]
    else:
        cmd += ["-vf", "format=yuv420p"]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", output]
    ffmpeg(cmd)

    if args.srt and segments:
        cues = []
        for seg in segments:
            if seg["end"] < start or seg["start"] > end:
                continue
            cues.append((max(seg["start"], start) - start, min(seg["end"], end) - start, seg["text"]))
        srt_path = os.path.splitext(output)[0] + ".srt"
        with open(srt_path, "w") as fh:
            for i, (s, e, text) in enumerate(cues, 1):
                fh.write(f"{i}\n{fmt_timecode(s)} --> {fmt_timecode(max(e, s + 0.4))}\n{text}\n\n")
        print(f"captions: {srt_path} ({len(cues)} cues)")
    elif args.srt:
        print("warning: --srt needs a library transcript for this source", file=sys.stderr)

    result = probe(output)
    print(f"{output}\n  {result.width}x{result.height}  {result.duration:.2f}s  "
          f"from {fmt_clock(start)}-{fmt_clock(end)}")
    if args.normalize:
        measured = loudness(output)
        if measured:
            print(f"  {float(measured.get('input_i', 0)):.1f} LUFS")

    if segments:
        quote = " ".join(s["text"] for s in segments if s["end"] > start and s["start"] < end)
        if quote:
            print(f'  "{quote[:220]}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
