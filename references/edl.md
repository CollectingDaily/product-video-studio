# The EDL schema

An EDL (edit decision list) is a single JSON file describing a whole short.
`render.py` turns it into a video. Paths inside it resolve relative to the EDL
file itself, so keep it beside the project.

## Contents

- [Top level](#top-level)
- [Shots](#shots)
- [Text on a shot](#text-on-a-shot)
- [Audio](#audio)
- [Captions](#captions)
- [The brand file](#the-brand-file)
- [Worked example](#worked-example)
- [How rendering works](#how-rendering-works)

## Top level

| Field | Default | Notes |
|---|---|---|
| `canvas` | `"9x16"` | `9x16`, `1x1`, `16x9`, `4x5`, or explicit `"1080x1350"`. `--canvas` overrides it. |
| `fps` | `30` | 30 suits social. Use 25 only to match existing PAL footage. |
| `brand` | none | Path to a brand json. Omitted means the built-in neutral look. |
| `transition` | `"cut"` | `cut` or `dip` (a short fade through the brand background between shots). |
| `audio` | silence | See [Audio](#audio). |
| `captions` | `{"mode":"burn"}` | See [Captions](#captions). |
| `shots` | required | The edit, in order. |

## Shots

Every shot has a `type`. Durations come from `duration` for stills and from
`in`/`out` for footage.

### `image`

```json
{ "type": "image", "src": "assets/hero.jpg", "duration": 3.2,
  "motion": "zoom_in", "fit": "auto", "text": { "headline": "..." } }
```

| Field | Default | Notes |
|---|---|---|
| `duration` | required | Seconds. 2.5-4 is the usable range. |
| `motion` | `"zoom_in"` | `none`, `zoom_in`, `zoom_out`, `pan_left`, `pan_right`, `pan_up`, `pan_down`. |
| `motion_amount` | `0.12` | How far it travels. Above ~0.25 it reads as a zoom effect rather than life. |
| `fit` | `"auto"` | See [fit](#fit). |

Stills are internally worked at twice the canvas size so the move resamples
from real detail. A source smaller than the canvas will still look soft —
check `probe_media.py` before relying on a small image.

### `video`

```json
{ "type": "video", "src": "assets/demo.mov", "in": 2.0, "out": 5.5,
  "speed": 1.0, "audio": false, "fit": "auto" }
```

| Field | Default | Notes |
|---|---|---|
| `in` | `0` | Seconds, or `"1:23"`, or `"01:02:03.5"`. |
| `out` | end of file | Same formats. |
| `speed` | `1.0` | `2.0` is twice as fast. Audio is pitch-corrected via `atempo`, valid 0.5-2.0. |
| `audio` | `false` for `video` | `true` keeps the clip's own sound; it then counts as speech for ducking. |
| `mute` | `false` | Force silence regardless. |
| `gain_db` | `0` | Trim this clip's level. |
| `transcribe` | `false` | Run whisper on this clip so it gets captions. Costs time; only for clips with speech that are not in the library. |

### `library`

Identical to `video`, but `audio` defaults to **true** and captions come free:
the words are already in the transcript, so no transcription happens at render
time.

```json
{ "type": "library", "src": "raw/interview.mov", "in": 412.3, "out": 419.8 }
```

`src` must be the same path recorded in `library/catalog.json`, which is what
`library_search.py` prints. If the path does not match, the clip still renders
but arrives without captions.

### `color`

A solid card, for an opening title or an end card.

```json
{ "type": "color", "color": "#101417", "duration": 2.5,
  "text": { "headline": "Five year warranty", "sub": "example.com", "position": "center" } }
```

### fit

How a source that is not the canvas shape is reconciled.

| Value | Behaviour |
|---|---|
| `auto` | Default. Crops when the shapes are within 25% of each other, otherwise blurs. |
| `cover` | Scale up and crop. Fills the frame, loses the edges. |
| `blur` | Scale to fit, fill the surround with a blurred, darkened copy. Nothing is lost. |
| `contain` | Scale to fit, pad with the brand background colour. Flat and clean. |

`auto` is right nearly always. Override it when the subject sits near an edge
and `cover` would cut it off, or when a brand forbids letterboxing.

## Text on a shot

```json
"text": {
  "headline": "Brushed anodised shell",
  "sub": "Scratch resistant",
  "position": "top",
  "panel": true,
  "size_ratio": 0.052,
  "margin_ratio": 0.10,
  "fade_in": 0.25,
  "sub_color": "#E8B33B",
  "max_chars_per_line": null
}
```

A bare string is shorthand for a headline: `"text": "Out now"`.

| Field | Default | Notes |
|---|---|---|
| `position` | brand's, usually `top` | `top`, `center`, `bottom`. |
| `panel` | `true` | A translucent box behind the type. Turn it off over a plain background. |
| `size_ratio` | brand's | Fraction of canvas **height**, so one value works across ratios. |
| `fade_in` | `0.25` | Seconds. `0` to appear instantly. |
| `max_chars_per_line` | auto | Only set this to force a specific line break. |

The type shrinks automatically if the longest line will not fit the frame, so
long headlines degrade gracefully rather than running off the edge. Even so,
a headline over about eight words is too long to read in three seconds.

Copy is passed to ffmpeg through a file, so apostrophes, colons, commas and
accents are all safe. No escaping needed.

## Audio

```json
"audio": {
  "mode": "music",
  "music": "music/bed.m4a",
  "music_gain_db": -9,
  "duck_db": -15,
  "fade_out": 1.2
}
```

| Field | Default | Notes |
|---|---|---|
| `mode` | `"silence"` | `music`, `silence`, `tts`. |
| `music` | — | Required for `music`. Loops automatically if shorter than the edit. |
| `music_gain_db` | `-8` | Level before ducking. |
| `duck_db` | `-14` | How far the bed drops under speech. `-10` is subtle, `-20` is firm. |
| `fade_out` | `1.2` | Seconds of fade at the end. |

Ducking is automatic and exact: every shot carrying speech defines an
interval, widened slightly so the dip happens in the gap rather than on a
word. No sidechain tuning required.

### TTS

```json
"audio": {
  "mode": "tts",
  "tts": { "text": "Meet the new collection...", "voice": "Daniel",
           "rate": 172, "start": 0.3, "gain_db": 0, "captions": true },
  "duck_db": -24
}
```

The narration is transcribed after generation so its captions carry real word
timings. Library clips keep their own audio and the synthetic voice ducks
under them — the real presenter should always win. `say -v '?'` lists voices.

A `music` key can be added alongside `tts` to get a bed as well.

## Captions

```json
"captions": { "mode": "burn" }
```

`burn` renders them into the picture and also writes an `.srt`. `srt` writes
only the sidecar. `none` writes nothing. `--captions` on the command line
overrides the file.

Cues come from library shots automatically, from `tts` narration, and from any
shot marked `"transcribe": true`. A shot with none of these contributes no
captions — which is correct for silent b-roll.

Cues are capped at two lines and about 3.2 seconds so they keep moving with
the speech at every aspect ratio. libass measures the font and wraps, so
captions cannot overflow the frame.

## The brand file

Copy `assets/brand.example.json`, fill it in, and point `brand` at it. Any
subset works — missing keys fall back to the defaults.

```json
{
  "font": "/Users/you/Library/Fonts/BrandSans-Bold.ttf",
  "font_regular": "/Users/you/Library/Fonts/BrandSans-Regular.ttf",
  "colors": { "text": "white", "accent": "#E8B33B",
              "background": "#101417", "panel": "black@0.55" },
  "caption": { "font_family": "Brand Sans", "font_size_ratio": 0.042,
               "margin_ratio": 0.13, "uppercase": false },
  "headline": { "font_size_ratio": 0.052, "position": "top" },
  "loudness_target_lufs": -14.0
}
```

`font` and `font_regular` are **file paths**, used by drawtext for headlines.
`caption.font_family` is an **installed family name**, used by libass for
captions — libass resolves by family, not by file. Get this wrong and
captions quietly fall back to a default face.

Colours accept `#RRGGBB`, a name, or `name@opacity` such as `black@0.55`.

## Worked example

```json
{
  "canvas": "9x16",
  "fps": 30,
  "brand": "brand.json",
  "transition": "dip",
  "audio": { "mode": "music", "music": "music/bed.m4a", "music_gain_db": -9, "duck_db": -15 },
  "captions": { "mode": "burn" },
  "shots": [
    { "type": "image", "src": "assets/hero.jpg", "duration": 3.0, "motion": "zoom_in",
      "text": { "headline": "The new Orlando collection", "sub": "Out now" } },
    { "type": "library", "src": "raw/interview.mov", "in": 6.19, "out": 14.65 },
    { "type": "image", "src": "assets/detail.jpg", "duration": 3.0, "motion": "pan_right",
      "text": { "headline": "Brushed anodised shell", "position": "bottom" } },
    { "type": "video", "src": "assets/demo.mov", "in": 1.0, "out": 4.5 },
    { "type": "image", "src": "assets/pack.jpg", "duration": 3.0, "motion": "zoom_out",
      "text": { "headline": "$499", "sub": "Same price as last season", "position": "center" } },
    { "type": "library", "src": "raw/interview.mov", "in": 47.98, "out": 52.97 },
    { "type": "color", "color": "#101417", "duration": 2.5,
      "text": { "headline": "Five year warranty", "sub": "example.com", "position": "center" } }
  ]
}
```

## How rendering works

Each shot is rendered to its own intermediate file at the exact canvas size,
frame rate and audio layout, then all of them are concatenated in one final
pass that mixes audio, burns captions and normalises loudness.

That matters when something looks wrong: `--keep-work` leaves the
intermediates on disk so you can play the single offending shot instead of
bisecting a long filter chain.

Validation runs before any encoding, so a missing file or an out point past
the end of a source is reported immediately rather than three minutes in.

### Timing

`--check` prints the timeline — per-shot duration, cumulative offset and
total — without rendering anything. Use it while iterating on structure.

Shot durations are quantised to whole frames, so the reported total can differ
from the arithmetic by a few hundredths of a second. The timeline used for
captions and ducking is measured from the rendered intermediates, not
calculated, so it is always exact.
