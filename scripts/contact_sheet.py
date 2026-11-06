#!/usr/bin/env python3
"""Build a labelled grid of frames so Claude can actually see the footage.

Claude cannot watch a video. Everything it knows about what is on screen comes
from still frames, so pulling a grid and looking at it is the difference
between choosing a shot and guessing at one. Use it to check which images are
worth using, find a clean thumbnail, or confirm that a clip shows the product
rather than the presenter's hand over the lens.

Each tile is labelled with its timecode (for a video) or filename (for a
folder), so a tile that looks right can be turned straight into an EDL shot.

Usage:
  contact_sheet.py interview.mp4 -o sheet.png              # 12 frames across the video
  contact_sheet.py interview.mp4 -o sheet.png -n 24
  contact_sheet.py interview.mp4 -o sheet.png --from 40 --to 70
  contact_sheet.py assets/ -o assets-sheet.png             # every image in a folder
  contact_sheet.py clip.mp4 --stills out/frames --at 3,8.5,12
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import IMAGE_EXTS, ToolError, ffmpeg, fmt_clock, probe, require, run  # noqa: E402

LABEL_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def collect_images(folder: str) -> list[str]:
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS and not name.startswith("."):
                found.append(os.path.join(root, name))
    return found


def grab_frames(src: str, times: list[float], outdir: str, width: int) -> list[tuple[str, str]]:
    tiles = []
    for i, t in enumerate(times):
        path = os.path.join(outdir, f"f{i:03d}.jpg")
        ffmpeg(["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
                "-vf", f"scale={width}:-2:flags=lanczos", "-q:v", "3", path])
        if os.path.exists(path):
            tiles.append((path, fmt_clock(t)))
    return tiles


def montage(tiles: list[tuple[str, str]], output: str, columns: int, width: int) -> None:
    require("magick")
    if not tiles:
        raise ToolError("No frames to lay out.")
    labelled = []
    tmpdir = os.path.dirname(tiles[0][0])
    for i, (path, label) in enumerate(tiles):
        out = os.path.join(tmpdir, f"L{i:03d}.png")
        # ImageMagick has no default font on a clean macOS install, so the
        # font file has to be named explicitly or -annotate/-label fails.
        run(["magick", path, "-resize", f"{width}x",
             "-background", "#1b1b1b", "-fill", "white",
             "-font", LABEL_FONT, "-pointsize", str(max(14, width // 16)),
             "-gravity", "south", "-splice", "0x28",
             "-annotate", "+0+4", label, out])
        labelled.append(out)
    rows = (len(labelled) + columns - 1) // columns
    run(["magick", "montage", *labelled, "-tile", f"{columns}x{rows}",
         "-geometry", "+6+6", "-background", "#111111", output])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="a video file or a folder of images")
    ap.add_argument("-o", "--output", help="contact sheet png")
    ap.add_argument("-n", "--count", type=int, default=12, help="frames to sample from a video")
    ap.add_argument("--from", dest="start", type=float, default=0.0, help="sample from this second")
    ap.add_argument("--to", dest="end", type=float, help="sample up to this second")
    ap.add_argument("--at", help="comma-separated timecodes instead of even sampling, e.g. 3,8.5,12")
    ap.add_argument("--columns", type=int, default=4)
    ap.add_argument("--tile-width", type=int, default=360)
    ap.add_argument("--stills", help="also write the full-size frames into this folder")
    args = ap.parse_args()

    source = os.path.abspath(os.path.expanduser(args.source))
    if not os.path.exists(source):
        print(f"error: not found: {source}", file=sys.stderr)
        return 1

    output = os.path.abspath(os.path.expanduser(
        args.output or os.path.splitext(source)[0] + "-sheet.png"
    ))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pvs-sheet-") as tmp:
        if os.path.isdir(source):
            images = collect_images(source)
            if not images:
                print(f"error: no images in {source}", file=sys.stderr)
                return 1
            tiles = [(p, os.path.basename(p)) for p in images]
            montage(tiles, output, args.columns, args.tile_width)
            print(f"{output}\n  {len(tiles)} images from {source}")
            return 0

        info = probe(source)
        if info.kind == "image":
            tiles = [(source, os.path.basename(source))]
            montage(tiles, output, 1, args.tile_width)
            print(output)
            return 0

        end = args.end if args.end is not None else info.duration
        if args.at:
            times = [float(t) for t in args.at.split(",")]
        else:
            span = max(0.0, end - args.start)
            # Offset by half a step so the first tile is not frame zero, which
            # on most recordings is a black or half-exposed frame.
            step = span / args.count
            times = [args.start + step * (i + 0.5) for i in range(args.count)]

        tiles = grab_frames(source, times, tmp, args.tile_width)
        montage(tiles, output, args.columns, args.tile_width)

        if args.stills:
            stills = os.path.abspath(os.path.expanduser(args.stills))
            os.makedirs(stills, exist_ok=True)
            for t in times:
                name = f"{os.path.splitext(os.path.basename(source))[0]}-{t:07.2f}.png".replace(".", "_", 1)
                ffmpeg(["-ss", f"{t:.3f}", "-i", source, "-frames:v", "1",
                        os.path.join(stills, name.replace("_", ".", 1))])
            print(f"full-size stills: {stills}")

        print(f"{output}\n  {len(tiles)} frames from {os.path.basename(source)} "
              f"({fmt_clock(args.start)}-{fmt_clock(end)})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
