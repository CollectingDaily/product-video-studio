#!/usr/bin/env python3
"""Add a long talking-head recording to the searchable library.

This is the step that makes everything else possible. Claude cannot watch or
listen to a two-hour interview, so the recording is turned into a word-timed
transcript. After that, "find where he explains the warranty" becomes a text
search that returns exact timecodes, and cutting a seven-second accent clip is
a precise operation rather than a guess.

Transcribing is the slow part (roughly 5-15 minutes per hour of audio on Apple
Silicon), so it happens once per recording and the result is cached. Re-running
on an already-ingested file is a no-op unless --force is passed.

Usage:
  library_ingest.py interview.mp4
  library_ingest.py interview.mp4 --title "Orlando launch interview" --tags warranty,pricing
  library_ingest.py "https://youtu.be/XXXX" --tags webinar
  library_ingest.py raw/*.mov --library ./library
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import (  # noqa: E402
    WHISPER_MODEL, ToolError, ensure_path, ffmpeg, fmt_clock, fmt_timecode,
    probe, require, run, slugify,
)

# A pause longer than this reads as the end of a thought, so it is a safe place
# to break a segment even when the speaker never lands on a full stop.
SENTENCE_GAP = 0.65
MAX_SEGMENT_CHARS = 180
SENTENCE_END = ".?!…"


def library_root(explicit: str | None) -> str:
    root = explicit or os.environ.get("PRODUCT_VIDEO_LIBRARY") or "library"
    root = os.path.abspath(os.path.expanduser(root))
    for sub in ("media", "transcripts"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def run_streaming(cmd: list[str], label: str) -> None:
    """Run a long job, letting its progress through to the terminal.

    Transcription can take many minutes; swallowing the output would make it
    look like the script had hung.
    """
    print(f"[{label}] {' '.join(cmd[:2])} ...", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        tail.append(line)
        del tail[:-40]
        stripped = line.strip()
        if "progress =" in stripped or stripped.startswith(("whisper_", "error", "failed")):
            print(f"  {stripped}", flush=True)
    code = proc.wait()
    if code != 0:
        raise ToolError(f"{label} failed ({code}):\n" + "".join(tail))


def fetch_url(url: str, media_dir: str) -> str:
    require("yt-dlp")
    print(f"[download] {url}")
    template = os.path.join(media_dir, "%(title).80s-%(id)s.%(ext)s")
    run(["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
         "--restrict-filenames", "--no-playlist", "--print", "after_move:filepath",
         "-o", template, url])
    # --print writes the final path to stdout; re-run quietly to read it back
    proc = run(["yt-dlp", "--no-playlist", "--restrict-filenames", "--get-filename",
                "-o", template, url])
    path = (proc.stdout or "").strip().splitlines()[-1]
    if not os.path.exists(path):
        candidates = [os.path.join(media_dir, f) for f in os.listdir(media_dir)]
        candidates = [c for c in candidates if os.path.isfile(c)]
        if not candidates:
            raise ToolError(f"Download reported success but no file found in {media_dir}")
        path = max(candidates, key=os.path.getmtime)
    return path


def extract_audio(src: str, wav: str, max_seconds: float | None) -> None:
    args = ["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    if max_seconds:
        args = ["-t", str(max_seconds)] + args
    ffmpeg(args + [wav])


def transcribe_words(wav: str, out_base: str, *, model: str, threads: int, language: str) -> list[dict]:
    """Return [{word, start, end}] using whisper.cpp.

    -ml 1 together with -sow makes every segment a single word, which is the
    reliable way to get word-level timings out of whisper.cpp in one pass.
    """
    require("whisper-cli")
    if not os.path.exists(model):
        raise ToolError(
            f"Whisper model not found at {model}\nDownload it once with:\n"
            "  mkdir -p ~/whisper-models && curl -L -o ~/whisper-models/ggml-large-v3-turbo.bin \\\n"
            "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
        )
    run_streaming(
        ["whisper-cli", "-m", model, "-f", wav, "-l", language,
         "-ml", "1", "-sow", "-oj", "-of", out_base,
         "-t", str(threads), "-pp", "-np"],
        "transcribe",
    )
    json_path = out_base + ".json"
    with open(json_path) as fh:
        data = json.load(fh)

    words: list[dict] = []
    for item in data.get("transcription", []):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        offsets = item.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        if end < start:
            end = start
        words.append({"w": text, "s": round(start, 3), "e": round(end, 3)})
    os.remove(json_path)
    return words


def group_segments(words: list[dict]) -> list[dict]:
    """Collapse words into sentence-ish segments that read well and search well.

    Searching single words gives no context, and searching the whole transcript
    gives no timecode. Sentences are the useful middle: long enough to judge
    whether a quote is worth using, short enough to cut on.
    """
    segments: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        segments.append({
            "start": current[0]["s"],
            "end": current[-1]["e"],
            "text": " ".join(w["w"] for w in current).strip(),
            "words": [[w["w"], w["s"], w["e"]] for w in current],
        })
        current.clear()

    for index, word in enumerate(words):
        current.append(word)
        text_len = sum(len(w["w"]) + 1 for w in current)
        gap_next = (words[index + 1]["s"] - word["e"]) if index + 1 < len(words) else 999.0
        ends_sentence = word["w"].rstrip('"”)').endswith(tuple(SENTENCE_END))
        if ends_sentence or gap_next >= SENTENCE_GAP or text_len >= MAX_SEGMENT_CHARS:
            flush()
    flush()
    return segments


def write_readable(path: str, title: str, segments: list[dict], duration: float) -> None:
    with open(path, "w") as fh:
        fh.write(f"# {title}\n")
        fh.write(f"# duration {fmt_clock(duration)}  segments {len(segments)}\n")
        fh.write("# format: [timecode] text   (timecode is the segment start)\n\n")
        for seg in segments:
            fh.write(f"[{fmt_clock(seg['start'])}] {seg['text']}\n")


def write_srt(path: str, segments: list[dict], *, max_chars: int = 84) -> None:
    """Caption cues for the full-length recording, split to a readable width."""
    cues: list[tuple[float, float, str]] = []
    for seg in segments:
        words = seg["words"]
        chunk: list[list] = []
        for word in words:
            chunk.append(word)
            if sum(len(w[0]) + 1 for w in chunk) >= max_chars:
                cues.append((chunk[0][1], chunk[-1][2], " ".join(w[0] for w in chunk)))
                chunk = []
        if chunk:
            cues.append((chunk[0][1], chunk[-1][2], " ".join(w[0] for w in chunk)))
    with open(path, "w") as fh:
        for i, (start, end, text) in enumerate(cues, 1):
            fh.write(f"{i}\n{fmt_timecode(start)} --> {fmt_timecode(max(end, start + 0.4))}\n{text}\n\n")


def load_catalog(root: str) -> dict:
    path = os.path.join(root, "catalog.json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {"sources": {}}


def save_catalog(root: str, catalog: dict) -> None:
    with open(os.path.join(root, "catalog.json"), "w") as fh:
        json.dump(catalog, fh, indent=2)


def rebuild_index(root: str, catalog: dict) -> int:
    """Rewrite index.jsonl from the per-source transcripts.

    Keeping the index derived rather than appended means removing a recording
    from the catalog cannot leave orphaned segments behind.
    """
    lines = 0
    with open(os.path.join(root, "index.jsonl"), "w") as out:
        for slug, meta in sorted(catalog["sources"].items()):
            words_path = os.path.join(root, "transcripts", f"{slug}.words.json")
            if not os.path.exists(words_path):
                continue
            with open(words_path) as fh:
                payload = json.load(fh)
            for i, seg in enumerate(payload["segments"]):
                out.write(json.dumps({
                    "slug": slug,
                    "title": meta.get("title", slug),
                    "src": meta["media"],
                    "tags": meta.get("tags", []),
                    "i": i,
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                }) + "\n")
                lines += 1
    return lines


def ingest_one(src: str, root: str, args) -> dict | None:
    media_dir = os.path.join(root, "media")
    if src.startswith(("http://", "https://")):
        src = fetch_url(src, media_dir)
    src = os.path.abspath(os.path.expanduser(src))
    if not os.path.exists(src):
        raise ToolError(f"Not found: {src}")

    title = args.title or os.path.splitext(os.path.basename(src))[0].replace("_", " ").replace("-", " ")
    slug = args.slug or slugify(title)
    catalog = load_catalog(root)
    words_path = os.path.join(root, "transcripts", f"{slug}.words.json")

    if os.path.exists(words_path) and not args.force:
        print(f"[skip] {slug} already transcribed (use --force to redo)")
        return None

    if args.copy:
        dest = os.path.join(media_dir, os.path.basename(src))
        if os.path.abspath(dest) != src:
            print(f"[copy] -> {dest}")
            import shutil
            shutil.copy2(src, dest)
        src = dest

    info = probe(src)
    print(f"[ingest] {title}  ({fmt_clock(info.duration)}, {info.kind})")
    if not info.has_audio:
        raise ToolError(f"{src} has no audio track, so there is nothing to transcribe.")

    with tempfile.TemporaryDirectory(prefix="pvs-ingest-") as tmp:
        wav = os.path.join(tmp, "audio.wav")
        print("[audio] extracting 16 kHz mono")
        extract_audio(src, wav, args.max_seconds)
        words = transcribe_words(
            wav, os.path.join(tmp, "out"),
            model=args.model, threads=args.threads, language=args.language,
        )

    if not words:
        raise ToolError("Transcription produced no words. Check that the recording actually contains speech.")
    segments = group_segments(words)
    print(f"[index] {len(words)} words -> {len(segments)} segments")

    with open(words_path, "w") as fh:
        json.dump({
            "slug": slug, "title": title, "src": src,
            "duration": info.duration, "language": args.language,
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "segments": segments,
        }, fh)

    write_readable(os.path.join(root, "transcripts", f"{slug}.txt"), title, segments, info.duration)
    write_srt(os.path.join(root, "transcripts", f"{slug}.srt"), segments)

    catalog["sources"][slug] = {
        "title": title,
        "media": src,
        "duration": round(info.duration, 2),
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
        "words": len(words),
        "segments": len(segments),
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_catalog(root, catalog)
    total = rebuild_index(root, catalog)
    print(f"[done] {slug}: transcript + {len(segments)} segments; library now holds {total} segments")
    return catalog["sources"][slug]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="video/audio files or URLs")
    ap.add_argument("--library", help="library root (default: ./library or $PRODUCT_VIDEO_LIBRARY)")
    ap.add_argument("--title", help="human title; defaults to the filename")
    ap.add_argument("--slug", help="override the generated id (only sensible with one source)")
    ap.add_argument("--tags", help="comma-separated tags, e.g. pricing,warranty,orlando")
    ap.add_argument("--copy", action="store_true", help="copy the media into library/media/ instead of referencing in place")
    ap.add_argument("--force", action="store_true", help="re-transcribe even if a transcript exists")
    ap.add_argument("--model", default=WHISPER_MODEL)
    ap.add_argument("--language", default="en")
    ap.add_argument("--threads", type=int, default=max(4, (os.cpu_count() or 8) - 2))
    ap.add_argument("--max-seconds", type=float, help="only transcribe the first N seconds (for a quick check)")
    args = ap.parse_args()

    ensure_path()
    root = library_root(args.library)
    if len(args.sources) > 1 and (args.title or args.slug):
        ap.error("--title/--slug apply to a single source; ingest multiple files one at a time")

    failures = 0
    for src in args.sources:
        try:
            ingest_one(src, root, args)
        except ToolError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
    print(f"\nLibrary: {root}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
