#!/usr/bin/env python3
"""Search the talking-head library for a quote and get cuttable timecodes back.

Ask for a meaning, not a filename: "why the warranty matters", "the bit about
shipping times". Matching is deliberately loose -- speech rarely contains the
exact phrase you had in mind, and a near miss with the right timecode is far
more useful than no result.

Each hit comes with a suggested in/out already snapped to word boundaries and
padded slightly, sized for an accent clip (4-9 seconds by default). Those
numbers drop straight into an EDL shot of type "library".

Usage:
  library_search.py "why the warranty matters"
  library_search.py "shipping" --tag pricing --limit 10
  library_search.py "best thing about it" --min 6 --max 12 --json
  library_search.py --list                       # what is in the library
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import fmt_clock  # noqa: E402

STOPWORDS = {
    "a", "an", "and", "the", "is", "it", "of", "to", "in", "on", "for", "that",
    "this", "with", "about", "i", "you", "we", "they", "he", "she", "at", "as",
    "be", "are", "was", "were", "so", "do", "does", "did", "but", "or", "if",
    "what", "when", "why", "how", "from", "my", "our", "your", "their",
}

# A little air either side stops a cut landing on the speaker's own breath.
PAD_HEAD = 0.18
PAD_TAIL = 0.32


def library_root(explicit: str | None) -> str:
    root = explicit or os.environ.get("PRODUCT_VIDEO_LIBRARY") or "library"
    return os.path.abspath(os.path.expanduser(root))


def load_index(root: str) -> list[dict]:
    path = os.path.join(root, "index.jsonl")
    if not os.path.exists(path):
        print(
            f"error: no library index at {path}\n"
            "Ingest a recording first:  library_ingest.py interview.mp4",
            file=sys.stderr,
        )
        raise SystemExit(2)
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower())]


# Crude but effective: people search for "ship" and the speaker said
# "shipping". Chopping common endings costs nothing and rescues most of those
# misses without pulling in a stemming library.
SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "ers", "er", "est", "ed", "ly", "es", "s")


def stem(word: str) -> str:
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)]
            # "shipping" -> "shipp" -> "ship"
            if len(base) > 3 and base[-1] == base[-2]:
                base = base[:-1]
            return base
    return word


def content_tokens(text: str) -> set[str]:
    return {stem(t) for t in tokens(text) if t not in STOPWORDS and len(t) > 2}


def score(query: str, text: str) -> float:
    """Blend three cheap signals so no single one can dominate.

    Exact phrase presence is the strongest evidence; shared content words catch
    paraphrase; the sequence ratio rewards similar wording and ordering.
    """
    q_lower, t_lower = query.lower(), text.lower()
    q_terms, t_terms = content_tokens(query), content_tokens(text)
    if not q_terms:
        q_terms = set(tokens(query))

    phrase = 1.0 if q_lower.strip() in t_lower else 0.0
    overlap = len(q_terms & t_terms) / len(q_terms) if q_terms else 0.0
    ratio = difflib.SequenceMatcher(None, q_lower, t_lower).ratio()
    partial = sum(1 for q in q_terms if any(q in t for t in t_terms)) / len(q_terms) if q_terms else 0.0

    return 0.45 * phrase + 0.35 * overlap + 0.1 * partial + 0.1 * ratio


def load_words(root: str, slug: str) -> list[dict]:
    path = os.path.join(root, "transcripts", f"{slug}.words.json")
    with open(path) as fh:
        return json.load(fh)["segments"]


def narrow_to_query(segment: dict, query: str, max_dur: float) -> tuple[float, float, str]:
    """Pick the best max_dur-long window of words inside an over-long segment."""
    words = segment["words"]
    if not words:
        return segment["start"], segment["end"], segment["text"]
    best = (-1.0, 0, len(words))
    for i in range(len(words)):
        for j in range(i + 1, len(words) + 1):
            if words[j - 1][2] - words[i][1] > max_dur:
                break
            text = " ".join(w[0] for w in words[i:j])
            value = score(query, text)
            if value > best[0]:
                best = (value, i, j)
    _, i, j = best
    chosen = words[i:j]
    return chosen[0][1], chosen[-1][2], " ".join(w[0] for w in chosen)


def build_window(root: str, hit: dict, query: str, min_dur: float, max_dur: float) -> dict:
    """Grow or shrink the matched segment into a clip of a usable length."""
    segments = load_words(root, hit["slug"])
    index = hit["i"]
    start, end = segments[index]["start"], segments[index]["end"]
    text = segments[index]["text"]

    if end - start > max_dur:
        start, end, text = narrow_to_query(segments[index], query, max_dur)
    else:
        lo = hi = index
        # Prefer extending forwards: a speaker's next sentence usually finishes
        # the thought, whereas the previous one sets up something we have cut.
        while end - start < min_dur:
            grew = False
            if hi + 1 < len(segments) and segments[hi + 1]["end"] - start <= max_dur:
                hi += 1
                end = segments[hi]["end"]
                grew = True
            elif lo - 1 >= 0 and end - segments[lo - 1]["start"] <= max_dur:
                lo -= 1
                start = segments[lo]["start"]
                grew = True
            if not grew:
                break
        text = " ".join(s["text"] for s in segments[lo : hi + 1])

    return {
        "slug": hit["slug"],
        "title": hit["title"],
        "src": hit["src"],
        "tags": hit["tags"],
        "score": round(hit["_score"], 3),
        "in": round(max(0.0, start - PAD_HEAD), 2),
        "out": round(end + PAD_TAIL, 2),
        "duration": round((end + PAD_TAIL) - max(0.0, start - PAD_HEAD), 2),
        "quote": text,
        "matched_segment": hit["text"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="what the speaker should be talking about")
    ap.add_argument("--library", help="library root (default: ./library or $PRODUCT_VIDEO_LIBRARY)")
    ap.add_argument("--tag", action="append", default=[], help="only sources carrying this tag (repeatable)")
    ap.add_argument("--slug", action="append", default=[], help="only these sources (repeatable)")
    ap.add_argument("--min", type=float, default=4.0, dest="min_dur", help="shortest acceptable clip (s)")
    ap.add_argument("--max", type=float, default=9.0, dest="max_dur", help="longest acceptable clip (s)")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--threshold", type=float, default=0.18, help="drop hits scoring below this")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", dest="list_sources", help="list library sources and exit")
    args = ap.parse_args()

    root = library_root(args.library)

    if args.list_sources:
        catalog_path = os.path.join(root, "catalog.json")
        if not os.path.exists(catalog_path):
            print(f"No library at {root}. Ingest a recording with library_ingest.py.", file=sys.stderr)
            return 2
        with open(catalog_path) as fh:
            catalog = json.load(fh)
        if args.json:
            print(json.dumps(catalog, indent=2))
            return 0
        print(f"Library: {root}\n")
        for slug, meta in sorted(catalog["sources"].items()):
            tags = ", ".join(meta.get("tags", [])) or "-"
            print(f"{slug:<34} {fmt_clock(meta['duration']):>9}  {meta['segments']:>5} seg  tags: {tags}")
            print(f"{'':34} {meta['media']}")
        return 0

    if not args.query:
        ap.error("a query is required (or use --list)")

    rows = load_index(root)
    if args.tag:
        wanted = {t.lower() for t in args.tag}
        rows = [r for r in rows if wanted & {t.lower() for t in r.get("tags", [])}]
    if args.slug:
        rows = [r for r in rows if r["slug"] in set(args.slug)]
    if not rows:
        print("No segments match those filters.", file=sys.stderr)
        return 1

    for row in rows:
        row["_score"] = score(args.query, row["text"])
    rows.sort(key=lambda r: r["_score"], reverse=True)

    hits = [r for r in rows if r["_score"] >= args.threshold][: args.limit]
    if not hits:
        print(f"Nothing above the {args.threshold} threshold. Closest matches were:", file=sys.stderr)
        for row in rows[:3]:
            print(f"  {row['_score']:.2f}  [{fmt_clock(row['start'])}] {row['text'][:150]}", file=sys.stderr)
        # Matching here is purely lexical, so a query like "what does it cost"
        # will never find "it ships at the same $499". When that happens the
        # transcript itself is the better tool -- read it and judge by meaning.
        slugs = sorted({r["slug"] for r in rows})
        print(
            "\nThis search only matches words, not meaning. For synonyms or paraphrase, read the\n"
            "timecoded transcript and choose the moment yourself:",
            file=sys.stderr,
        )
        for slug in slugs[:5]:
            print(f"  {os.path.join(root, 'transcripts', slug + '.txt')}", file=sys.stderr)
        return 1

    # Keep one hit per source region: adjacent segments often all match and
    # would otherwise fill the results with near-duplicates of one moment.
    windows: list[dict] = []
    claimed: list[tuple[str, float, float]] = []
    for hit in hits:
        window = build_window(root, hit, args.query, args.min_dur, args.max_dur)
        if any(window["slug"] == s and window["in"] < o and window["out"] > i for s, i, o in claimed):
            continue
        claimed.append((window["slug"], window["in"], window["out"]))
        windows.append(window)

    if args.json:
        print(json.dumps([{k: v for k, v in w.items() if not k.startswith("_")} for w in windows], indent=2))
        return 0

    print(f'Query: "{args.query}"   library: {root}\n')
    for n, w in enumerate(windows, 1):
        print(f"{n}. {w['title']}  [{fmt_clock(w['in'])} -> {fmt_clock(w['out'])}]  {w['duration']}s  score {w['score']}")
        print(f'   "{w["quote"]}"')
        print(f"   shot: {{\"type\":\"library\",\"src\":\"{w['src']}\",\"in\":{w['in']},\"out\":{w['out']}}}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
