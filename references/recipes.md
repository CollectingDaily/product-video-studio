# Direct ffmpeg, ImageMagick and yt-dlp

For the things the scripts do not cover. Everything here assumes:

```bash
export PATH="/opt/homebrew/bin:$PATH"
```

## Contents

- [Two things that bite on this machine](#two-things-that-bite-on-this-machine)
- [Reframing and resizing](#reframing-and-resizing)
- [Trimming and joining](#trimming-and-joining)
- [Text and captions](#text-and-captions)
- [Audio](#audio)
- [Stills, thumbnails and GIFs](#stills-thumbnails-and-gifs)
- [whisper-cli directly](#whisper-cli-directly)
- [yt-dlp](#yt-dlp)
- [Encoding settings](#encoding-settings)
- [Troubleshooting](#troubleshooting)

## Two things that bite on this machine

**PATH.** `/opt/homebrew/bin` is added by `~/.zprofile`, which only login
shells read. A non-interactive shell gets "command not found" for ffmpeg even
though it is installed. Export it, every time.

**ImageMagick fonts.** There is no default font configured, so any `magick`
command using `-annotate`, `-label` or `-draw text` fails with
`unable to read font` unless given `-font /path/to/font.ttf`. Arial Bold lives
at `/System/Library/Fonts/Supplemental/Arial Bold.ttf`.

## Reframing and resizing

Crop to fill — fills the frame, loses the edges:

```bash
ffmpeg -i in.mp4 -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1" \
  -c:a copy out.mp4
```

Fit with a blurred surround — nothing is lost, usually the better look for
off-ratio product shots:

```bash
ffmpeg -i in.mp4 -filter_complex \
"[0:v]split=2[bg][fg];\
 [bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28,eq=brightness=-0.12[bgb];\
 [fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fgs];\
 [bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]" \
  -map "[v]" -map 0:a? -c:a copy out.mp4
```

Fit with flat padding:

```bash
ffmpeg -i in.mp4 -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=#101417,setsar=1" out.mp4
```

Crop off-centre, when the subject is not in the middle — `crop=w:h:x:y`:

```bash
ffmpeg -i in.mp4 -vf "scale=-2:1920,crop=1080:1920:300:0,setsar=1" out.mp4
```

## Trimming and joining

Fast, frame-accurate cut (re-encodes):

```bash
ffmpeg -ss 412.3 -t 7.5 -i in.mp4 -c:v libx264 -crf 18 -c:a aac out.mp4
```

Lossless cut (instant, but lands on the nearest keyframe, so it can be off by
a second or two):

```bash
ffmpeg -ss 412 -t 8 -i in.mp4 -c copy out.mp4
```

Join files that share codec, resolution and frame rate:

```bash
printf "file '%s'\n" /abs/a.mp4 /abs/b.mp4 > list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4
```

Join files that do not — the concat filter re-encodes and needs matching
geometry, so normalise first:

```bash
ffmpeg -i a.mp4 -i b.mp4 -filter_complex \
  "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]" -map "[v]" -map "[a]" out.mp4
```

Crossfade between two clips (`offset` = when the fade starts, in output time):

```bash
ffmpeg -i a.mp4 -i b.mp4 -filter_complex \
  "[0:v][1:v]xfade=transition=fade:duration=0.4:offset=4.6[v];\
   [0:a][1:a]acrossfade=d=0.4[a]" -map "[v]" -map "[a]" out.mp4
```

Speed change, picture and sound together:

```bash
ffmpeg -i in.mp4 -filter_complex "[0:v]setpts=PTS/2[v];[0:a]atempo=2.0[a]" \
  -map "[v]" -map "[a]" out.mp4
```

`atempo` only accepts 0.5-2.0; chain it for more (`atempo=2.0,atempo=2.0`).

## Text and captions

**Pass text through a file.** A filtergraph is unescaped twice before the
filter sees it, so apostrophes, colons and commas in ordinary copy either
break the graph or arrive mangled. `textfile=` avoids the whole problem:

```bash
printf 'Five year warranty\nParts and labour' > /tmp/line.txt
ffmpeg -i in.mp4 -vf "drawtext=fontfile='/System/Library/Fonts/Supplemental/Arial Bold.ttf':\
textfile='/tmp/line.txt':fontcolor=white:fontsize=96:x=(w-text_w)/2:y=h*0.1:\
line_spacing=10:box=1:boxcolor=black@0.55:boxborderw=40" out.mp4
```

drawtext cannot wrap. Break the lines yourself and keep the longest under
roughly `frame_width / (fontsize * 0.58)` characters.

Burn an existing subtitle file:

```bash
ffmpeg -i in.mp4 -vf "subtitles=captions.srt:force_style='FontName=Arial,Fontsize=24,\
PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=180'" out.mp4
```

ASS gives far more control and libass measures the font, so it wraps properly:

```bash
ffmpeg -i in.mp4 -vf "ass=captions.ass" out.mp4
```

In `force_style` and ASS files, colours are `&HAABBGGRR` — alpha first, then
**blue green red**, and alpha is inverted (`00` is opaque).

Overlay a logo, 22% of the frame width, bottom right inside the safe area:

```bash
ffmpeg -i in.mp4 -i logo.png -filter_complex \
  "[1:v]scale=iw*0.22:-1[lg];[0:v][lg]overlay=W-w-60:H-h-200" out.mp4
```

## Audio

Measure before changing anything:

```bash
ffmpeg -i in.mp4 -af loudnorm=print_format=json -f null -
```

Normalise to the platform target:

```bash
ffmpeg -i in.mp4 -af "loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.97" -c:v copy out.mp4
```

-14 LUFS suits social; -16 is the podcast convention.

Duck music under speech over known intervals — deterministic, unlike a
sidechain compressor, and the intervals are already known from the edit:

```bash
ffmpeg -i music.mp3 -af "volume='if(gt(between(t,3.8,8.2)+between(t,15.1,21.4),0),0.2,1)':eval=frame" out.wav
```

Sidechain ducking, when the intervals are not known:

```bash
ffmpeg -i music.mp3 -i voice.wav -filter_complex \
  "[0:a][1:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[a]" -map "[a]" out.wav
```

Mix a bed under a voice track:

```bash
ffmpeg -i voice.wav -i music.mp3 -filter_complex \
  "[1:a]volume=-9dB[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]" -map "[a]" out.wav
```

Clean up a noisy recording:

```bash
ffmpeg -i in.wav -af "highpass=f=80,afftdn=nr=12:nf=-28,loudnorm=I=-16" out.wav
```

Strip audio, or replace it:

```bash
ffmpeg -i in.mp4 -an out.mp4
ffmpeg -i in.mp4 -i new.wav -map 0:v -map 1:a -c:v copy -shortest out.mp4
```

macOS text to speech — note that `say` takes the container from
`--file-format`, not the extension, and refuses little-endian samples in an
AIFF:

```bash
say -v Daniel -r 172 -o out.wav --file-format=WAVE --data-format=LEI16@22050 "Your line here"
say -v '?'     # list voices
```

## Stills, thumbnails and GIFs

One frame at a timecode:

```bash
ffmpeg -ss 12.5 -i in.mp4 -frames:v 1 -q:v 2 frame.jpg
```

A frame every five seconds:

```bash
ffmpeg -i in.mp4 -vf "fps=1/5,scale=480:-2" frames/f%04d.jpg
```

The most representative frames, chosen by ffmpeg — good for thumbnails:

```bash
ffmpeg -i in.mp4 -vf "thumbnail=300,scale=640:-2" -frames:v 5 -vsync 0 thumb%02d.png
```

Contact sheet in one pass (`contact_sheet.py` does this with labels):

```bash
ffmpeg -i in.mp4 -vf "fps=1/10,scale=320:-2,tile=4x3" -frames:v 1 sheet.png
```

Title card with ImageMagick — remember the explicit `-font`:

```bash
magick -size 1080x1920 gradient:'#101417-#1d2a33' \
  -font "/System/Library/Fonts/Supplemental/Arial Bold.ttf" \
  -fill white -gravity center -pointsize 96 -annotate +0-100 'Five year warranty' \
  -fill '#E8B33B' -pointsize 52 -annotate +0+40 'example.com' card.png
```

A good GIF needs a palette, or it will band badly:

```bash
ffmpeg -i in.mp4 -vf "fps=12,scale=480:-1:flags=lanczos,palettegen" -y /tmp/pal.png
ffmpeg -i in.mp4 -i /tmp/pal.png -lavfi "fps=12,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse" out.gif
```

## whisper-cli directly

The scripts cover the normal path. Direct use is for one-offs:

```bash
whisper-cli -m ~/whisper-models/ggml-large-v3-turbo.bin -f audio.wav -l en -osrt -of out
```

Input must be 16 kHz mono WAV (or flac/mp3/ogg) — extract it first:

```bash
ffmpeg -i in.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le audio.wav
```

| Flag | Effect |
|---|---|
| `-ml 1 -sow` | One word per segment. This is how word-level timings are obtained in a single pass. |
| `-oj` / `-ojf` | JSON output / JSON with extra detail. |
| `-osrt` `-ovtt` `-otxt` `-ocsv` | Other output formats. |
| `--prompt "..."` | Seed vocabulary. Feed it product and people names to stop them being mangled. |
| `-t N` | Threads. |
| `--vad` | Voice activity detection; helps on recordings with long silences. |

```bash
whisper-cli -m ~/whisper-models/ggml-large-v3-turbo.bin -f audio.wav \
  --prompt "Orlando collection, anodised, PFC Marketing" -ml 1 -sow -oj -of out
```

## yt-dlp

Only on footage you have the right to use.

```bash
yt-dlp -f "bv*+ba/b" --merge-output-format mp4 -o "raw/%(title).80s-%(id)s.%(ext)s" URL
yt-dlp -F URL                       # list available formats
yt-dlp -x --audio-format wav URL    # audio only
yt-dlp --download-sections "*120-300" URL   # just part of it
```

## Encoding settings

| Use | Flags |
|---|---|
| Final delivery | `-c:v libx264 -preset slow -crf 19 -pix_fmt yuv420p -movflags +faststart` |
| Draft | `-c:v libx264 -preset ultrafast -crf 30` |
| Fast hardware encode | `-c:v h264_videotoolbox -b:v 8M` — much quicker, slightly worse per bit |
| Intermediate, high quality | `-c:v prores_videotoolbox -profile:v 3` |
| Audio | `-c:a aac -b:a 192k -ar 48000 -ac 2` |

`-pix_fmt yuv420p` is not optional for anything a browser or phone will play.
`+faststart` moves the index to the front so playback starts before the file
has fully downloaded.

Lower CRF is better quality: 18 is visually lossless, 23 is the default, 28
is noticeably soft.

## Troubleshooting

**"command not found: ffmpeg"** — PATH. Export `/opt/homebrew/bin`.

**"unable to read font"** — ImageMagick needs an explicit `-font`.

**"No such filter: 'drawtext'"** — the plain Homebrew ffmpeg was installed
instead of the full build. Fix with
`brew uninstall ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg`.

**Filtergraph parse errors around text** — use `textfile=` rather than
escaping.

**Output plays as green or will not play** — add `-pix_fmt yuv420p`.

**Audio and picture drift after concatenation** — the inputs differ in frame
rate or sample rate. Normalise each one first; this is exactly why `render.py`
renders per-shot intermediates.

**A cut lands in the wrong place** — `-c copy` snaps to keyframes. Re-encode
for frame accuracy.

**Captions in the wrong font** — libass resolves by installed family name, not
by file path. Check `caption.font_family` against the family the font really
has.

**zoompan judders** — the source is too small for the move. Stills are worked
at twice the canvas internally; an image smaller than the canvas cannot be
rescued.
