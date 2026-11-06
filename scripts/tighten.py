#!/usr/bin/env python3
"""Strip the dead air out of a recording with auto-editor.

Useful in two places. Before ingesting a long recording, it removes the gaps
where nobody is speaking, which makes the transcript tighter and the clips
easier to find. And on a finished talking clip, it closes the pauses so a
seven-second answer becomes a five-second one that still says everything.

Pauses are detected by loudness, not by meaning, so the defaults here are
deliberately gentle: margin keeps a little air either side of each phrase, and
without it the result sounds like someone gasping for breath.

Beware of one thing: cutting a recording changes every timecode in it. Tighten
first, then ingest -- never the other way round, or the library's timecodes
will point at the wrong moments.

Usage:
  tighten.py raw/interview.mov -o raw/interview-tight.mov
  tighten.py clip.mp4 -o clip-tight.mp4 --margin 0.1 --threshold 3%
  tighten.py raw/interview.mov -o out.mov --report     # what would be cut
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import ToolError, fmt_clock, probe, require, run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("-o", "--output", help="output file (default: <name>-tight.<ext>)")
    ap.add_argument("--threshold", default="4%",
                    help="loudness below this counts as silence (default 4%%; raise it for a noisy room)")
    ap.add_argument("--margin", default="0.2s",
                    help="air kept either side of speech (default 0.2s; lower is tighter but more clipped)")
    ap.add_argument("--report", action="store_true", help="print what would change and stop")
    ap.add_argument("--extra", nargs=argparse.REMAINDER,
                    help="anything after this is passed straight to auto-editor")
    args = ap.parse_args()

    require("auto-editor")
    src = os.path.abspath(os.path.expanduser(args.source))
    if not os.path.exists(src):
        print(f"error: not found: {src}", file=sys.stderr)
        return 1

    before = probe(src)
    stem, ext = os.path.splitext(src)
    output = os.path.abspath(os.path.expanduser(args.output or f"{stem}-tight{ext}"))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    cmd = ["auto-editor", src, "--edit", f"audio:threshold={args.threshold}",
           "--margin", args.margin, "-o", output, "--no-open"]
    if args.extra:
        cmd += [a for a in args.extra if a != "--"]
    if args.report:
        cmd = ["auto-editor", "info", src]
        proc = run(cmd, check=False)
        print(proc.stdout or proc.stderr)
        return 0

    print(f"[tighten] {os.path.basename(src)}  {fmt_clock(before.duration)} ...", flush=True)
    run(cmd)

    if not os.path.exists(output):
        raise ToolError("auto-editor reported success but produced no file.")
    after = probe(output)
    saved = before.duration - after.duration
    pct = (saved / before.duration * 100) if before.duration else 0
    print(f"{output}\n  {fmt_clock(before.duration)} -> {fmt_clock(after.duration)} "
          f"(removed {fmt_clock(max(0, saved))}, {pct:.0f}%)")
    if pct > 45:
        print("  note: that is a lot to remove. Check it still sounds natural, "
              "or raise --margin / lower --threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
