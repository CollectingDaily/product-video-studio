---
name: product-video-studio
description: >-
  Produce 30-60 second product videos on a Mac from supplied photos, clips,
  articles and newsletters, cutting in short accent quotes from a searchable
  library of long-form talking-head recordings. Everything runs locally with
  ffmpeg, whisper.cpp, auto-editor and ImageMagick; nothing is uploaded.
  Use this skill whenever the user wants a short video, reel, social cut,
  promo, teaser, product clip, vertical video or "something for Instagram"
  built from their own assets; whenever they want a long interview, webinar or
  talk transcribed, indexed, or searched for the moment someone said a
  particular thing; and whenever they mention b-roll, captions, subtitles,
  burned-in text, contact sheets, thumbnails, Ken Burns, reframing to 9:16 /
  1:1 / 16:9, removing silences or dead air, or turning an article or
  newsletter into a video. Reach for it even when the request sounds like a
  single ffmpeg command, because the duration, framing and caption decisions
  it encodes are what separate a usable cut from a technically correct one.
argument-hint: "<what you want made, e.g. a 45s vertical cut about the new range>"
---

# Product video studio

Two halves that fit together:

1. **The library.** Long talking-head recordings get transcribed once, word by
   word, into a searchable index. After that you can find the moment someone
   explained the warranty and get back exact timecodes.
2. **The short.** Product photos, clips and an article become a 30-60 second
   video, with a few seconds of that library footage cut in as punctuation.

You cannot see or hear any of this footage. Every judgement you make rests on
transcripts, on still frames you pull out, and on measurements. Pull them.
Guessing at what is in a shot produces confident, wrong edits.

## The tools

ffmpeg, ffprobe, whisper-cli, auto-editor, yt-dlp and ImageMagick are already
installed via Homebrew. They live in `/opt/homebrew/bin`, which is on PATH for
login shells only — so a bare `ffmpeg` in a non-interactive shell fails with
"command not found". The bundled scripts fix PATH themselves. If you run
ffmpeg directly, export it first:

```bash
export PATH="/opt/homebrew/bin:$PATH"
```

ImageMagick has no default font configured on this machine. Any `magick`
command that draws text needs an explicit `-font /path/to/font.ttf` or it
errors out.

All scripts are in `scripts/` next to this file and take `--help`.

## Part 1 — building the library

One command per recording. Transcription is the slow part, roughly a third of
real time on Apple Silicon, so a two-hour interview takes around 40 minutes.
It happens once and is cached; re-running is a no-op without `--force`.

```bash
python3 scripts/library_ingest.py raw/interview.mov \
  --title "Orlando launch interview" --tags orlando,warranty,pricing
```

That writes, under `library/`:

- `transcripts/<slug>.words.json` — every word with its start and end
- `transcripts/<slug>.txt` — the same thing readable, with a timecode per sentence
- `transcripts/<slug>.srt` — captions for the full recording
- `index.jsonl` — one searchable line per sentence
- `catalog.json` — what is in the library

Tag generously at ingest time. Tags are the cheapest way to narrow a search
later, and nobody wants to re-transcribe to add them.

If a recording is full of long pauses, tighten it **before** ingesting —
cutting it afterwards invalidates every timecode in the index:

```bash
python3 scripts/tighten.py raw/interview.mov -o raw/interview-tight.mov
```

### Finding a moment

```bash
python3 scripts/library_search.py "why the warranty matters" --min 4 --max 9
python3 scripts/library_search.py "shipping" --tag pricing --json
python3 scripts/library_search.py --list
```

Each hit comes back with an in/out already snapped to word boundaries and
sized for an accent clip, ready to paste into an EDL.

**When search finds nothing, read the transcript.** Matching is lexical, so
"what does it cost" will never find "it ships at the same $499", and "is it
noisy" will never find "how quiet it runs". You are the semantic layer the
search lacks: open `library/transcripts/<slug>.txt`, read it, and pick the
timecode yourself. For a large library, search first to narrow to a source,
then read that one transcript.

## Part 2 — making a short

### Step 1: Look at what you were given

```bash
python3 scripts/probe_media.py assets/
python3 scripts/contact_sheet.py assets/ -o work/assets.png
python3 scripts/contact_sheet.py assets/demo.mov -o work/demo.png -n 12
```

Then actually read the contact sheet image. Dimensions tell you which stills
will need a blurred surround in a vertical frame; the frames tell you which
clips show the product and which show someone adjusting a tripod. A shot list
written without looking is a guess.

### Step 2: Find the one idea

Read the article or newsletter. A 45-second video carries **one** claim, not a
summary. Pick the single most specific, most surprising, most concrete thing
in the source — a number, a guarantee, a reversal — and build everything
around it. "Five year warranty, parts and labour, no registration" beats "our
commitment to quality" every time, and the source nearly always contains one
of these buried in paragraph four.

Everything that does not serve that one claim gets cut. That is the job.

### Step 3: Write the beat sheet before the EDL

Four beats, written as plain text first so the structure can be judged before
any rendering happens:

| Beat | Length | Job |
|---|---|---|
| Hook | 2-4s | Earn the next three seconds. A claim, a number, or the product doing something. |
| Proof | 15-25s | Why it is true. Where the detail shots and the talking clips live. |
| Payoff | 5-8s | The one idea, stated plainly. Usually the strongest library quote. |
| Close | 2-4s | What to do next, and where. |

Show the user the beat sheet before you render. It is far cheaper to fix a
structural problem in a paragraph than in a 40-second encode.

### Step 4: Cast the talking clips

This is an **assets-first** edit. Product imagery and on-screen text carry the
video; the presenter punctuates it. Concretely:

- **1-3 library clips**, 4-9 seconds each, no more than about a third of the
  total runtime. Four talking clips in a 45-second video is a talking-head
  video with pictures in it, which is a different thing.
- Put a talking clip where a human voice earns its place: a claim that needs a
  person behind it, an opinion, a guarantee. Not for facts that on-screen text
  states faster.
- Let the speaker finish the thought. A clip that ends mid-sentence reads as a
  mistake, not as pace. `library_search.py` snaps to word boundaries; use
  `extract_clip.py --snap` if you need sentence boundaries.

### Step 5: Write the EDL

The whole edit is one JSON file. See `references/edl.md` for the full schema;
the short version:

```json
{
  "canvas": "9x16",
  "fps": 30,
  "brand": "brand.json",
  "transition": "dip",
  "audio": { "mode": "music", "music": "music/bed.m4a", "duck_db": -15 },
  "captions": { "mode": "burn" },
  "shots": [
    { "type": "image", "src": "assets/hero.jpg", "duration": 3.0, "motion": "zoom_in",
      "text": { "headline": "The new Orlando collection", "sub": "Out now" } },
    { "type": "library", "src": "raw/interview.mov", "in": 412.3, "out": 419.8 },
    { "type": "video", "src": "assets/demo.mov", "in": 2.0, "out": 5.5 },
    { "type": "color", "color": "#101417", "duration": 2.5,
      "text": { "headline": "Five year warranty", "sub": "example.com", "position": "center" } }
  ]
}
```

Writing the plan down means an edit can be corrected in one line and
re-rendered identically, and the same EDL can be re-cut to another aspect
ratio with `--canvas`. Keep the EDL next to the output; it is the edit.

### Step 6: Check, preview, then look

```bash
python3 scripts/render.py edl.json --check                      # timings only, instant
python3 scripts/render.py edl.json -o out/preview.mp4 --preview # half size, ~15s
```

Then pull frames from the preview and **look at them**:

```bash
python3 scripts/contact_sheet.py out/preview.mp4 -o work/check.png -n 8
```

Check the things that only a picture reveals: text inside the frame and clear
of the platform's UI, the product visible rather than cropped out, captions
not covering it, no shot overstaying.

### Step 7: Export

```bash
python3 scripts/render.py edl.json -o out/orlando-9x16.mp4
python3 scripts/render.py edl.json -o out/orlando-1x1.mp4  --canvas 1x1
python3 scripts/render.py edl.json -o out/orlando-16x9.mp4 --canvas 16x9
```

An `.srt` is written beside every output whether or not captions are burned
in. Audio lands at -14 LUFS, which is what the platforms normalise to.

Re-cutting to a second ratio is nearly free, but it is not automatic: text
that sits comfortably in a 9:16 frame can collide with a 16:9 one. Pull frames
from each ratio you ship.

## What makes the difference

**Ask about captions.** Do not assume. Most feeds autoplay muted, so burned-in
captions usually win — but they also cover the product, and some brands will
not have text baked into the picture. Ask which the user wants for this video,
then set `captions.mode` to `burn`, `srt` or `none`.

**Pick the audio mode deliberately.**

| Mode | When |
|---|---|
| `music` | The default. A bed under the whole video, ducking automatically under every talking clip. Needs a track in `music/`. |
| `silence` | Product video that has its own sound worth hearing, or a brand that does not use music. Captions carry the message. |
| `tts` | Draft narration from macOS `say` when there is no suitable library clip. Fine for judging pacing, audibly synthetic for anything public — say so rather than shipping it quietly. |

**Hold the duration.** 30-60 seconds. Stills want 2.5-4 seconds — under 2 and
the eye cannot land, over 5 and attention goes. If the beat sheet will not fit,
cut a beat rather than speeding everything up. `--check` reports the total
before you render.

**Respect the safe area.** On 9:16, the bottom ~15% and top ~10% are covered
by platform UI. The brand file's margins already account for this; do not
push text outside them to fit more in.

**Vary the motion.** Every still zooming in at the same rate reads as a
slideshow. Alternate `zoom_in`, `zoom_out` and a pan, and let a static shot
sit where the voice is doing the work.

**Never overwrite an original.** Write new files, into `out/`. The sources are
often the only copy.

## Reference files

- `references/edl.md` — the full EDL schema, every field and what it does
- `references/library.md` — ingest, search and transcript formats in detail
- `references/recipes.md` — direct ffmpeg and ImageMagick for anything the
  scripts do not cover, plus yt-dlp and troubleshooting
- `assets/brand.example.json` — a brand file to copy and fill in

## Scripts

| Script | What it is for |
|---|---|
| `probe_media.py` | Duration, dimensions, fps, audio, loudness. Run first, on everything. |
| `contact_sheet.py` | A labelled grid of frames. This is how you see the footage. |
| `library_ingest.py` | Transcribe a recording into the library. Once per file. |
| `library_search.py` | Find a quote, get cuttable timecodes. |
| `extract_clip.py` | Cut one clip out, optionally snapped, reframed and captioned. |
| `render.py` | Render an EDL. `--check`, `--preview`, `--canvas`. |
| `tighten.py` | Remove silences with auto-editor. Before ingest, never after. |
