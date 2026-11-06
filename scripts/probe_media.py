#!/usr/bin/env python3
"""Report the facts about media files: duration, dimensions, fps, audio, loudness.

Claude cannot see or hear a file, so this is how it learns what it is working
with before planning an edit. Run it on every supplied asset first -- the
dimensions decide whether a shot needs cropping or padding, and the durations
decide what can realistically fit in 30-60 seconds.

Usage:
  probe_media.py assets/*.jpg assets/*.mp4
  probe_media.py --loudness interview.mp4
  probe_media.py --json assets/            # a directory is scanned recursively
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import IMAGE_EXTS, fmt_clock, loudness, probe  # noqa: E402

MEDIA_EXTS = IMAGE_EXTS | {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".caf",
}


def expand(paths: list[str]) -> list[str]:
    found: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in MEDIA_EXTS and not name.startswith("."):
                        found.append(os.path.join(root, name))
        else:
            found.append(path)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="files or directories")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--loudness", action="store_true",
                    help="also measure integrated loudness (slow: decodes the whole file)")
    args = ap.parse_args()

    results = []
    for path in expand(args.paths):
        if not os.path.exists(path):
            results.append({"path": path, "error": "not found"})
            continue
        try:
            info = probe(path).as_dict()
            if args.loudness and info["kind"] != "image" and info["has_audio"]:
                measured = loudness(path)
                if measured:
                    info["input_lufs"] = float(measured.get("input_i", 0) or 0)
                    info["input_true_peak_db"] = float(measured.get("input_tp", 0) or 0)
            results.append(info)
        except Exception as exc:  # a bad file should not stop the rest
            results.append({"path": path, "error": str(exc)})

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    for info in results:
        name = os.path.basename(info["path"])
        if "error" in info:
            print(f"{name:<40} ERROR  {info['error']}")
            continue
        bits = [f"{info['kind']:<5}"]
        if info["width"]:
            ratio = info["aspect"]
            shape = "vertical" if ratio < 0.95 else ("square" if ratio < 1.05 else "landscape")
            bits.append(f"{info['width']}x{info['height']} ({shape} {ratio:.2f})")
        if info["kind"] != "image":
            bits.append(f"{fmt_clock(info['duration'])}")
            bits.append(f"{info['fps']:g}fps" if info["fps"] else "")
            bits.append("audio" if info["has_audio"] else "NO AUDIO")
        if "input_lufs" in info:
            bits.append(f"{info['input_lufs']:.1f} LUFS")
        print(f"{name:<40} " + "  ".join(b for b in bits if b))

    images = sum(1 for i in results if i.get("kind") == "image")
    videos = sum(1 for i in results if i.get("kind") == "video")
    total = sum(i.get("duration", 0) for i in results if i.get("kind") in ("video", "audio"))
    print(f"\n{len(results)} file(s): {images} image, {videos} video; {fmt_clock(total)} of footage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
