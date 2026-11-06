# Product Video Studio

A [Claude Code](https://claude.com/claude-code) skill for building 30–60 second
product videos on a Mac, from your own photos, clips and copy — plus a
searchable, word-accurate index of your long-form talking-head recordings to
cut quotes out of.

Everything runs locally. Nothing is uploaded, transcribed in the cloud, or sent
to a third party.

```
long interview ──► transcribe once ──► search by meaning ──► exact timecodes
                                                                    │
product photos ──┐                                                  ▼
       b-roll  ──┼──► EDL (one json file) ──► render ──► 16:9 · 9:16 · 1:1
   brand sheet ──┘                                      captions · −14 LUFS
```

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installing the skill](#installing-the-skill)
- [Quickstart](#quickstart)
- [How a job actually goes](#how-a-job-actually-goes)
- [The scripts](#the-scripts)
- [The EDL](#the-edl)
- [Project layout](#project-layout)
- [Hard-won notes](#hard-won-notes)
- [Worked example](#worked-example)
- [Repository layout](#repository-layout)
- [Licence](#licence)

---

## What it does

Two halves that fit together.

**The library.** Long recordings — interviews, webinars, talks — get
transcribed once, word by word, into a searchable index. After that you can ask
for the moment someone explained the warranty and get back timecodes already
snapped to word boundaries, ready to paste into an edit.

**The short.** Product photos, clips and an article become a 30–60 second video
with a few seconds of that library footage cut in as punctuation. Captions are
burned from real word timings, audio lands at −14 LUFS, and the same edit
re-cuts to any aspect ratio.

The skill is deliberately **assets-first**: product imagery and on-screen text
carry the video, the presenter punctuates it. Four talking clips in a
45-second video is a talking-head video with pictures in it, which is a
different thing.

---

## Requirements

### macOS and Homebrew

Built and tested on Apple Silicon. The shared module hardcodes
`BREW_BIN = "/opt/homebrew/bin"`; on an Intel Mac change that one line in
`scripts/mediakit.py` to `/usr/local/bin`.

### Command-line tools

```bash
brew install ffmpeg whisper-cpp auto-editor yt-dlp imagemagick
```

| Tool | Used for | Verified against |
|---|---|---|
| `ffmpeg` / `ffprobe` | every render, probe, key, crop and loudness pass | 9.0.2 |
| `whisper-cli` (whisper-cpp) | word-level transcription for the library | 1.9.5 |
| `auto-editor` | removing silences before ingest | 31.8.0 |
| `yt-dlp` | pulling a recording straight from a URL | 2026.10.26 |
| `magick` (ImageMagick 7) | title cards, crops, composites, contact sheets | 7.1.2-35 |
| `say` | draft TTS narration (ships with macOS) | — |

Python 3.9 or newer, from the system or Homebrew. No pip packages, no virtual
environment — the scripts use the standard library only.

### The whisper model

One 1.5 GB download, once:

```bash
mkdir -p ~/whisper-models && curl -L -o ~/whisper-models/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

That path is the default. Point `--model` elsewhere if you keep it somewhere
else. Transcription runs at roughly a third of real time on Apple Silicon, so
a two-hour interview takes about 40 minutes — once, then it is cached.

### Fonts

ImageMagick on macOS has **no default font configured**, so every `magick`
command that draws text needs an explicit `-font /path/to/font.ttf` or it errors
out. The brand file wants two font *file paths* for headlines and one installed
*family name* for captions — see [Hard-won notes](#hard-won-notes), because
getting that distinction wrong fails silently.

### Optional

A music bed in `music/` as `.m4a` or `.wav`. Without one, set
`audio.mode` to `silence` and let the captions carry the message.

---

## Installing the skill

Clone into your Claude Code skills directory:

```bash
git clone https://github.com/CollectingDaily/product-video-studio.git ~/.claude/skills/product-video-studio
```

Claude Code picks it up on the next session. Confirm with `/product-video-studio`,
or just describe the video you want — the skill's description is written to
trigger on reels, social cuts, b-roll, captions, reframing, transcription and
"something for Instagram".

To use the scripts on their own, with no Claude Code involved, they are plain
Python and every one takes `--help`.

---

## Quickstart

```bash
# 1 — index a long recording (once per file; slow, cached)
python3 scripts/library_ingest.py raw/interview.mov \
  --title "Orlando launch interview" --tags orlando,warranty,pricing
```

```bash
# 2 — find a usable quote, get cuttable timecodes
python3 scripts/library_search.py "why the warranty matters" --min 4 --max 9
```

```bash
# 3 — check the edit's timings without encoding anything
python3 scripts/render.py edl.json --check
```

```bash
# 4 — export
python3 scripts/render.py edl.json -o out/orlando-16x9.mp4 --canvas 16x9
```

An `.srt` is written beside every output whether or not captions are burned in.

---

## How a job actually goes

The skill walks Claude through seven steps. They matter in order.

**1. Look at what you were given.** `probe_media.py` for dimensions and
loudness, `contact_sheet.py` for a labelled grid of frames — then *actually look
at the sheet*. A shot list written without looking is a guess. Dimensions tell
you which stills need a blurred surround in a vertical frame; the frames tell
you which clips show the product and which show someone adjusting a tripod.

**2. Find the one idea.** A 45-second video carries one claim, not a summary.
Pick the most specific, most concrete thing in the source — a number, a
guarantee, a reversal. "Five year warranty, parts and labour, no registration"
beats "our commitment to quality", and the source nearly always has one buried
in paragraph four. Everything that does not serve it gets cut.

**3. Write the beat sheet before the EDL.**

| Beat | Length | Job |
|---|---|---|
| Hook | 2–4s | Earn the next three seconds. A claim, a number, the product doing something. |
| Proof | 15–25s | Why it is true. Where detail shots and talking clips live. |
| Payoff | 5–8s | The one idea, stated plainly. Usually the strongest quote. |
| Close | 2–4s | What to do next, and where. |

It is far cheaper to fix a structural problem in a paragraph than in a
40-second encode.

**4. Cast the talking clips.** One to three library clips, 4–9 seconds each, no
more than about a third of the runtime. Put one where a human voice earns its
place — a claim that needs a person behind it, an opinion, a guarantee — not
for facts that on-screen text states faster. Let the speaker finish the thought;
a clip that ends mid-sentence reads as a mistake, not as pace.

**5. Write the EDL.** One json file, [below](#the-edl).

**6. Check, preview, then look.** `--check` prints the timeline instantly.
`--preview` renders at half size in about fifteen seconds. Then pull frames out
of the preview and look at them: text inside the frame and clear of the
platform's UI, product visible rather than cropped out, captions not covering
it, no shot overstaying.

**7. Export.** Re-cutting to a second ratio is nearly free but it is not
automatic — text that sits comfortably in 9:16 can collide in 16:9. Pull frames
from every ratio you ship.

Two standing rules: hold the duration between 30 and 60 seconds, and **never
overwrite an original**. Write new files, into `out/`. The sources are often
the only copy.

---

## The scripts

All in `scripts/`, all take `--help`.

| Script | What it is for |
|---|---|
| `probe_media.py` | Duration, dimensions, fps, audio, loudness. Run first, on everything. |
| `contact_sheet.py` | A labelled grid of frames. This is how you see the footage. |
| `library_ingest.py` | Transcribe a recording into the library. Once per file. |
| `library_search.py` | Find a quote, get cuttable timecodes. `--list` shows the library. |
| `extract_clip.py` | Cut one clip out, optionally snapped, reframed and captioned. |
| `render.py` | Render an EDL. `--check`, `--preview`, `--canvas`. |
| `tighten.py` | Remove silences with auto-editor. Before ingest, never after. |
| `mediakit.py` | Shared helpers. Not run directly. |

`PRODUCT_VIDEO_LIBRARY` overrides the library root, which otherwise defaults to
`./library`.

### Searching is lexical, not semantic

`library_search.py` matches words, so "what does it cost" will never find
"it ships at the same $499". When search comes back empty, open
`library/transcripts/<slug>.txt` and read it — the timecodes are in there. For
a large library, search to narrow to a source, then read that one transcript.

---

## The EDL

An edit decision list is one json file describing a whole short. Paths inside
it resolve relative to the file itself, so keep it beside the project. The
full schema — every field, every default — is in
[`references/edl.md`](references/edl.md).

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

Four shot types: `image` (Ken Burns over a still), `video` (your own footage),
`library` (indexed footage — audio and captions come free), and `color` (a
solid card for a title or an end frame).

Canvases: `9x16`, `1x1`, `16x9`, `4x5`, or an explicit `"1080x1350"`.

Writing the plan down means an edit can be corrected in one line and
re-rendered identically. Keep the EDL next to the output; it *is* the edit.

### Audio modes

| Mode | When |
|---|---|
| `music` | The default. A bed under the whole video, ducking automatically under every talking clip. |
| `silence` | Product video with its own sound worth hearing, or a brand that does not use music. |
| `tts` | Draft narration from macOS `say`. Fine for judging pacing, audibly synthetic for anything public. |

Ducking is exact rather than sidechained: every shot carrying speech defines an
interval, widened slightly so the dip happens in the gap rather than on a word.

### Captions

`burn` renders them into the picture and writes an `.srt`; `srt` writes only
the sidecar; `none` writes nothing. Cues come free from library shots, from
`tts` narration, and from any shot marked `"transcribe": true`. They are capped
at two lines and about 3.2 seconds, and libass measures the font and wraps, so
captions cannot overflow the frame.

Most feeds autoplay muted, so burned-in captions usually win — but they also
cover the product, and some brands will not have text baked into the picture.
Worth asking before you render.

---

## Project layout

A job is a directory. The convention:

```
my-video/
  brand.json          # fonts, colours, margins, loudness target
  edl.json            # the edit
  assets/             # stills, title cards, b-roll clips you prepared
  raw/                # source recordings (never written to)
  library/            # transcripts + index, built by library_ingest.py
  music/              # bed, if any
  work/               # intermediates, contact sheets, scratch
  out/                # deliverables + .srt sidecars
```

Copy [`assets/brand.example.json`](assets/brand.example.json) to `brand.json`
and fill it in. Any subset works — missing keys fall back to sensible defaults.

---

## Hard-won notes

Things that cost real time to discover. Each one fails in a way that looks like
a broken render rather than a parameter half a step out.

**PATH.** Homebrew's `/opt/homebrew/bin` is on PATH for *login* shells only, so
a bare `ffmpeg` in a non-interactive shell fails with "command not found". The
bundled scripts fix this themselves. If you run ffmpeg directly:

```bash
export PATH="/opt/homebrew/bin:$PATH"
```

**ImageMagick has no default font.** Any `magick` command that draws text needs
an explicit `-font /path/to/font.ttf`.

**Two kinds of font setting, and they are not interchangeable.** In the brand
file, `font` and `font_regular` are **file paths**, used by drawtext for
headlines. `caption.font_family` is an **installed family name**, used by libass
for captions, because libass resolves by family and not by file. Get it wrong
and captions quietly fall back to a default face.

**libass can resolve a family to an italic face.** Asking for "Avenir Next" got
italic captions throughout. Name a specific style instead —
`"font_family": "Avenir Next Demi Bold"` with `"bold": false`.

**`caption.font_size_ratio` is a fraction of canvas *height*.** One value
cannot serve every ratio: 0.055 suits 16:9 and 1:1, but in a tall 9:16 frame
the same number produces enormous type — use about 0.034 there.

**Whisper mishears short clips.** Re-transcribing a five-second cutaway gets
words wrong that it gets right in the full recording ("telegrams" became
"testing"). Slice caption words out of the master transcript instead, assigning
each word to the clip its midpoint falls in, rather than setting
`"transcribe": true` on a fragment.

**Snap in and out points to word edges.** Caption lookup uses an inclusive
overlap test, so an in point a few hundredths of a second inside a word drags
that whole word into the cue — which shows up as a duplicated or bleeding
caption. The exact edges are in `library/transcripts/<slug>.words.json`.

**Tighten before ingest, never after.** Cutting a recording after indexing
invalidates every timecode in the index.

**Loudness.** `render.py` normalises to −14 LUFS, which is what the platforms
target. If a single-pass result lands short, measure with `ebur128` and apply a
static gain of `target − I` plus a limiter; two-pass `loudnorm` tends to
overshoot on sparse speech.

**Keying a green screen.** ffmpeg's `chromakey` similarity metric is
normalised, so values that sound small are enormous — above about 0.15 it takes
a blue shirt with it. `chromakey=0x38BE6C:0.11:0.04` with
`despill=type=green:mix=0.2:expand=0.0` works on a curtain-and-table setup;
despill above about 0.3 turns white cotton and paper pink. Crop the wall and
light stands out *before* keying, and key once per aspect ratio rather than
reframing one keyed master — cropping a 16:9 key down to 9:16 loses the hands
and the table.

**`--keep-work`** leaves the per-shot intermediates on disk, so when something
looks wrong you can play the single offending shot instead of bisecting a long
filter chain.

---

## Worked example

The first real job this skill shipped: a 60-second piece on an 1882 Great
Britain £5 Orange stamp, built from one 3½-minute green-screen presenter
recording, two 8192×5464 photographs and a logo.

- Presenter keyed off green and composited onto a navy radial gradient, keyed
  separately for each of the three ratios so his hands and the table survived
  every frame.
- Five b-roll clips cut from crops of the two photographs, each carrying its
  own slice of the presenter's audio so the voice runs unbroken underneath and
  captions never stop.
- Title and end cards set in Didot over the same navy.
- Logo overlaid top-right at a per-ratio size, dropped before the end card.
- Delivered 16:9, 9:16 and 1:1 — each 59.9s, 25 fps, −14.4 LUFS / −1.0 dBFS,
  with burned captions and an `.srt`.

The vertical cut needed its own treatment rather than a crop: a 9:16 window on
an 8192×5464 photograph leaves the stamp no margin, so the whole-stamp shots
were composited onto velvet lifted from the same frame, and captions were sized
and raised to clear the platform UI.

---

## Repository layout

```
SKILL.md                      the skill itself — what Claude reads
README.md                     this file
scripts/                      eight python tools, standard library only
references/
  edl.md                      the full EDL schema, every field
  library.md                  ingest, search and transcript formats
  recipes.md                  direct ffmpeg and ImageMagick for anything
                              the scripts do not cover, plus troubleshooting
assets/
  brand.example.json          copy, fill in, point `brand` at it
```

---

## Licence

MIT. See [LICENSE](LICENSE).
