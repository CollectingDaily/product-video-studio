# The talking-head library

## Contents

- [What it is for](#what-it-is-for)
- [Layout on disk](#layout-on-disk)
- [Ingesting](#ingesting)
- [Searching](#searching)
- [When search fails](#when-search-fails)
- [File formats](#file-formats)
- [Maintenance](#maintenance)

## What it is for

A couple of hours of someone talking about products, recorded once, becomes a
pool of 4-9 second clips you can draw on for months. The transcript is what
makes it a pool rather than a pile: without word-level timecodes, finding "the
bit where he explains the warranty" means scrubbing, and you cannot scrub.

The trade is a one-off cost. Transcribing runs at roughly a third of real
time on Apple Silicon — about 40 minutes for a two-hour recording — and is
cached permanently.

## Layout on disk

```
library/
├── catalog.json                       what is in the library
├── index.jsonl                        one line per sentence, searchable
├── media/                             only files ingested with --copy
└── transcripts/
    ├── <slug>.words.json              every word with start and end
    ├── <slug>.txt                     readable, timecode per sentence
    └── <slug>.srt                     captions for the full recording
```

By default the media stays where it is and the catalog records its absolute
path — large recordings are not copied. Pass `--copy` to bring a file into
`library/media/`, which is worth it for anything you intend to keep.

`index.jsonl` is derived from the transcripts and rebuilt on every ingest, so
it never holds stale entries.

Set `PRODUCT_VIDEO_LIBRARY` to use a library outside the current folder, or
pass `--library` to each script. Otherwise `./library` is assumed.

## Ingesting

```bash
python3 scripts/library_ingest.py raw/interview.mov \
  --title "Orlando launch interview" \
  --tags orlando,warranty,pricing,objections
```

| Option | Notes |
|---|---|
| `--title` | Human name. Defaults to the filename. Also drives the slug. |
| `--tags` | Comma separated. Tag generously — it is the cheapest way to narrow a search later. |
| `--copy` | Copy the media into `library/media/` rather than referencing it in place. |
| `--force` | Re-transcribe something already ingested. |
| `--max-seconds` | Transcribe only the first N seconds. Good for checking a long file before committing to it. |
| `--language` | Defaults to `en`. Use `auto` to detect. |
| `--threads` | Defaults to CPU count minus two. |

A URL works too, via yt-dlp — only on footage you have the right to use:

```bash
python3 scripts/library_ingest.py "https://youtu.be/XXXX" --tags webinar
```

### Tighten before you ingest, never after

`tighten.py` removes silences, which shifts every timecode after the first
cut. Done after ingesting, the index points at the wrong moments and there is
no warning — the clips simply come out wrong.

```bash
python3 scripts/tighten.py raw/interview.mov -o raw/interview-tight.mov
python3 scripts/library_ingest.py raw/interview-tight.mov --title "..."
```

### Accuracy

The model is Whisper large-v3-turbo, which handles accents and background
noise well. It still mangles unusual product names, and it normalises spoken
numbers inconsistently — "five year warranty" may come back as "5 year
warranty". Search for both forms, or search for the surrounding words.

To help it with names, pass an initial prompt through whisper directly; see
`recipes.md`.

## Searching

```bash
python3 scripts/library_search.py "why the warranty matters"
python3 scripts/library_search.py "shipping" --tag pricing --limit 10
python3 scripts/library_search.py "best thing about it" --min 6 --max 12 --json
python3 scripts/library_search.py --list
```

| Option | Default | Notes |
|---|---|---|
| `--min` / `--max` | 4 / 9 | Target clip length. The window grows into neighbouring sentences to reach `--min`, and narrows within a long sentence to stay under `--max`. |
| `--tag` / `--slug` | all | Repeatable filters. |
| `--threshold` | 0.18 | Lower it to see weaker matches. |
| `--limit` | 6 | |
| `--json` | | Machine-readable, for pasting into an EDL. |

Scoring blends exact phrase presence, shared content words after light
stemming, and sequence similarity. Overlapping hits are collapsed so one
moment does not fill the results.

Each hit returns an in/out already padded slightly and snapped to word
boundaries — ready to drop into a `library` shot.

## When search fails

Matching is lexical. It has no idea that "cost" relates to "$499", or that
"noisy" is the opposite of "quiet". When a sensible query returns nothing, the
answer is not a cleverer query — it is to read the transcript:

```bash
cat library/transcripts/orlando-launch-interview.txt
```

Every line carries its start timecode, so a line you like converts directly
into a shot. You are the semantic layer the index lacks; a 2-hour transcript
is only around 20,000 words and reads quickly.

For a large library, search first to find the likely source, then read that
one transcript rather than all of them.

## File formats

### `catalog.json`

```json
{
  "sources": {
    "orlando-launch-interview": {
      "title": "Orlando launch interview",
      "media": "/absolute/path/to/interview.mov",
      "duration": 3612.4,
      "width": 1920, "height": 1080, "fps": 25.0,
      "tags": ["orlando", "warranty"],
      "words": 9841, "segments": 612,
      "ingested_at": "2026-11-06T11:13:30+00:00"
    }
  }
}
```

### `<slug>.words.json`

```json
{
  "slug": "orlando-launch-interview",
  "title": "Orlando launch interview",
  "src": "/absolute/path/to/interview.mov",
  "duration": 3612.4,
  "segments": [
    { "start": 6.37, "end": 8.9,
      "text": "The first thing people notice is the finish.",
      "words": [["The", 6.37, 6.51], ["first", 6.51, 6.84]] }
  ]
}
```

Segments break on terminal punctuation, on a pause of 0.65s or more, or at
180 characters — whichever comes first. That keeps them long enough to judge
and short enough to cut on.

### `index.jsonl`

One object per line: `slug`, `title`, `src`, `tags`, `i` (segment index),
`start`, `end`, `text`. Being line-delimited, it greps usefully:

```bash
grep -i warranty library/index.jsonl | head
```

## Maintenance

To remove a recording, delete its three files from `transcripts/`, drop its
entry from `catalog.json`, then re-ingest anything else to rebuild
`index.jsonl` — or rebuild by hand:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from library_ingest import load_catalog, rebuild_index
root = 'library'
print(rebuild_index(root, load_catalog(root)), 'segments')"
```

To re-transcribe everything after a model change, run ingest with `--force`
over each source in the catalog.
