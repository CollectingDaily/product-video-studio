#!/usr/bin/env python3
"""Render a short product video from an edit decision list (EDL).

The EDL is a JSON file describing the whole edit: canvas, shots in order, the
text on each, where the audio comes from and how captions are handled. Writing
the plan down first and rendering from it means an edit can be reviewed,
corrected in one line and re-rendered identically -- much better than a pile of
one-off ffmpeg commands nobody can reconstruct.

See references/edl.md for the full schema.

Each shot is first normalised to its own intermediate file at the exact canvas
size, frame rate and audio layout. That is slightly slower than one giant
filtergraph, but when something looks wrong you can play the single offending
intermediate instead of bisecting a 400-character filter chain.

Usage:
  render.py edl.json -o out/clip-9x16.mp4
  render.py edl.json -o out/preview.mp4 --preview      # fast, half-size
  render.py edl.json --check                           # validate, render nothing
  render.py edl.json -o out/square.mp4 --canvas 1x1    # re-cut for another ratio
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mediakit import (  # noqa: E402
    ToolError, ass_color, canvas_size, escape_drawtext, escape_filter_path,
    ffmpeg, fmt_ass_time, fmt_timecode, load_brand, parse_time, probe,
    require, run, wrap_text,
)

DUCK_PAD = 0.2          # widen ducking either side so the dip lands in silence
DIP_DURATION = 0.18     # length of a fade-through-colour transition
AUDIO_RATE = 48000


# ------------------------------------------------------------------ EDL load


def load_edl(path: str) -> dict:
    with open(path) as fh:
        edl = json.load(fh)
    if "shots" not in edl or not isinstance(edl["shots"], list) or not edl["shots"]:
        raise ToolError("EDL needs a non-empty 'shots' array.")
    return edl


def resolve(path: str, base_dir: str) -> str:
    """Resolve an asset path relative to the EDL file, which is how a human
    naturally writes it, while still honouring absolute paths."""
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    candidate = os.path.join(base_dir, expanded)
    return candidate if os.path.exists(candidate) else expanded


def validate(edl: dict, base_dir: str) -> list[str]:
    problems: list[str] = []
    for n, shot in enumerate(edl["shots"], 1):
        kind = shot.get("type")
        if kind not in ("image", "video", "library", "color"):
            problems.append(f"shot {n}: unknown type {kind!r} (use image, video, library or color)")
            continue
        if kind == "color":
            if not shot.get("duration"):
                problems.append(f"shot {n}: a color shot needs a duration")
            continue
        src = shot.get("src")
        if not src:
            problems.append(f"shot {n}: missing 'src'")
            continue
        full = resolve(src, base_dir)
        if not os.path.exists(full):
            problems.append(f"shot {n}: file not found: {src}")
            continue
        if kind == "image":
            if not shot.get("duration"):
                problems.append(f"shot {n}: an image shot needs a duration")
        else:
            info = probe(full)
            start = parse_time(shot.get("in", 0))
            end = parse_time(shot["out"]) if shot.get("out") is not None else info.duration
            if end <= start:
                problems.append(f"shot {n}: out ({end}) must be after in ({start})")
            if info.duration and end > info.duration + 0.1:
                problems.append(
                    f"shot {n}: out {end:.2f}s is past the end of {os.path.basename(full)} "
                    f"({info.duration:.2f}s)"
                )
    audio = edl.get("audio") or {}
    mode = audio.get("mode", "silence")
    if mode not in ("silence", "music", "tts"):
        problems.append(f"audio.mode {mode!r} must be silence, music or tts")
    if mode == "music" and not audio.get("music"):
        problems.append("audio.mode is 'music' but no audio.music path was given")
    if mode == "music" and audio.get("music") and not os.path.exists(resolve(audio["music"], base_dir)):
        problems.append(f"music file not found: {audio['music']}")
    if mode == "tts" and not (audio.get("tts") or {}).get("text"):
        problems.append("audio.mode is 'tts' but audio.tts.text is empty")
    caps = (edl.get("captions") or {}).get("mode", "burn")
    if caps not in ("burn", "srt", "none"):
        problems.append(f"captions.mode {caps!r} must be burn, srt or none")
    return problems


# --------------------------------------------------------------- shot filters


def fit_mode(shot: dict, src_aspect: float, canvas_aspect: float) -> str:
    """Choose how to reconcile a source that is not the canvas shape.

    Cropping a landscape product photo into a 9:16 frame throws away most of
    the product, so when the shapes disagree badly the default is to letterbox
    against a blurred copy of the image: nothing is lost and the frame still
    fills.
    """
    requested = shot.get("fit", "auto")
    if requested != "auto":
        return requested
    if not src_aspect or not canvas_aspect:
        return "cover"
    ratio = src_aspect / canvas_aspect
    return "cover" if 0.8 <= ratio <= 1.25 else "blur"


def geometry_chain(fit: str, w: int, h: int, bg: str) -> str:
    if fit == "cover":
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h},setsar=1"
        )
    if fit in ("contain", "pad"):
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1"
        )
    if fit == "blur":
        # Two copies of the same source: one cropped and blurred to fill the
        # frame, the original scaled to fit and laid on top.
        return (
            f"split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
            f"crop={w}:{h},gblur=sigma=28,eq=brightness=-0.12[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    raise ToolError(f"unknown fit {fit!r} (use auto, cover, contain or blur)")


def zoompan_chain(shot: dict, w: int, h: int, fps: int, frames: int) -> str:
    """Ken Burns move for a still.

    The expressions are written in terms of the output frame number rather than
    the previous frame's zoom, so the move is exactly as long as the shot and
    cannot drift.
    """
    motion = shot.get("motion", "zoom_in")
    amount = float(shot.get("motion_amount", 0.12))
    last = max(1, frames - 1)
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

    if motion in ("none", "static"):
        z, x, y = "1", centre_x, centre_y
    elif motion == "zoom_in":
        z, x, y = f"1+{amount}*on/{last}", centre_x, centre_y
    elif motion == "zoom_out":
        z, x, y = f"{1 + amount}-{amount}*on/{last}", centre_x, centre_y
    elif motion in ("pan_left", "pan_right"):
        z = f"{1 + amount}"
        progress = f"on/{last}" if motion == "pan_right" else f"(1-on/{last})"
        x, y = f"(iw-iw/zoom)*{progress}", centre_y
    elif motion in ("pan_up", "pan_down"):
        z = f"{1 + amount}"
        progress = f"on/{last}" if motion == "pan_down" else f"(1-on/{last})"
        x, y = centre_x, f"(ih-ih/zoom)*{progress}"
    else:
        raise ToolError(
            f"unknown motion {motion!r} (use none, zoom_in, zoom_out, pan_left, pan_right, pan_up, pan_down)"
        )
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps}"


# Average glyph advance as a fraction of the point size, for a bold grotesque.
# Deliberately pessimistic: drawtext cannot wrap or shrink text by itself, so
# over-estimating costs a slightly early line break while under-estimating
# pushes the headline off the side of the frame.
CHAR_WIDTH = 0.58


def layout_text(text: str, max_width: int, size: int, min_size: int) -> tuple[int, list[str]]:
    """Wrap text and shrink the type until the longest line fits the frame."""
    for _ in range(14):
        per_line = max(6, int(max_width / (size * CHAR_WIDTH)))
        lines = wrap_text(text, per_line)
        longest = max((len(line) for line in lines), default=0)
        if longest * size * CHAR_WIDTH <= max_width or size <= min_size:
            return size, lines
        size = max(min_size, int(size * 0.92))
    return size, wrap_text(text, max(6, int(max_width / (size * CHAR_WIDTH))))


def text_chain(shot: dict, brand: dict, w: int, h: int, duration: float,
               work: str, index: int) -> str:
    """drawtext layers for a shot's headline and sub-line.

    The copy is handed to drawtext through textfile= rather than text=. A
    filtergraph is unescaped twice before the filter sees it, so an apostrophe,
    a colon, a comma or a line break in ordinary marketing copy would other-
    wise either break the graph or arrive mangled. A file sidesteps all of it.
    """
    spec = shot.get("text")
    if not spec:
        return ""
    if isinstance(spec, str):
        spec = {"headline": spec}

    font = brand.get("font") or ""
    font_regular = brand.get("font_regular") or font
    colors = brand["colors"]
    conf = brand["headline"]
    position = spec.get("position", conf.get("position", "top"))
    panel = spec.get("panel", True)
    safe = brand.get("safe_area_ratio", 0.06)

    headline = spec.get("headline") or spec.get("title")
    sub = spec.get("sub") or spec.get("subtitle")

    usable = int(w * (1 - 2 * max(safe, 0.05)))
    if panel:
        usable = int(usable * 0.9)  # the panel adds padding around the glyphs
    base = int(h * float(spec.get("size_ratio", conf["font_size_ratio"])))
    cap = spec.get("max_chars_per_line") or conf.get("max_chars_per_line")

    head_size, head_lines = layout_text(headline, usable, base, int(base * 0.45)) if headline else (base, [])
    if cap and head_lines:
        head_lines = wrap_text(headline, min(cap, max(6, int(usable / (head_size * CHAR_WIDTH)))))
    sub_base = int(head_size * 0.58)
    sub_size, sub_lines = layout_text(sub, usable, sub_base, int(sub_base * 0.5)) if sub else (sub_base, [])

    # drawtext has no line-height control beyond line_spacing, so block height
    # has to be worked out here to stack the headline and sub-line.
    spacing = 10
    head_h = len(head_lines) * (head_size + spacing)
    sub_h = len(sub_lines) * (sub_size + spacing)
    pad = int(head_size * 0.45) if panel else 0
    margin = max(int(h * float(spec.get("margin_ratio", conf["margin_ratio"]))), int(h * safe))

    if position == "top":
        head_y = margin
        sub_y = margin + head_h + pad * 2 + int(head_size * 0.25)
    elif position == "center":
        block_h = head_h + sub_h + (int(head_size * 0.25) if sub_lines else 0)
        head_y = (h - block_h) // 2
        sub_y = head_y + head_h + int(head_size * 0.25) + (pad * 2 if panel else 0)
    else:  # bottom
        block_h = head_h + sub_h + (int(head_size * 0.25) if sub_lines else 0) + pad * 2
        head_y = h - margin - block_h
        sub_y = head_y + head_h + int(head_size * 0.25) + (pad * 2 if panel else 0)

    fade_in = float(spec.get("fade_in", 0.25))

    def block(lines: list[str], size: int, font_path: str, y: int, color: str, role: str) -> str:
        text_path = os.path.join(work, f"text{index:03d}-{role}.txt")
        with open(text_path, "w") as fh:
            fh.write("\n".join(lines))
        parts = [
            f"drawtext=fontfile='{escape_filter_path(font_path)}'",
            f"textfile='{escape_filter_path(text_path)}'",
            f"fontcolor={color}",
            f"fontsize={size}",
            "x=(w-text_w)/2",
            f"y={max(0, y)}",
            f"line_spacing={spacing}",
            f"shadowx=2:shadowy=3:shadowcolor={colors['text_shadow']}",
        ]
        if panel:
            parts += ["box=1", f"boxcolor={colors['panel']}", f"boxborderw={int(size*0.45)}"]
        if fade_in > 0:
            parts.append(f"alpha='if(lt(t,{fade_in}),t/{fade_in},1)'")
        return ":".join(parts)

    layers: list[str] = []
    if head_lines:
        layers.append(block(head_lines, head_size, font, head_y, colors["text"], "head"))
    if sub_lines:
        layers.append(block(sub_lines, sub_size, font_regular, sub_y,
                            spec.get("sub_color", colors["accent"]), "sub"))
    return ",".join(layers)


def dip_chain(index: int, total: int, duration: float, style: str, color: str) -> str:
    """Fade a shot in/out so consecutive shots dip through a colour."""
    if style != "dip" or duration <= DIP_DURATION * 2.5:
        return ""
    parts = []
    if index > 0:
        parts.append(f"fade=t=in:st=0:d={DIP_DURATION}:color={color}")
    if index < total - 1:
        parts.append(f"fade=t=out:st={duration - DIP_DURATION:.3f}:d={DIP_DURATION}:color={color}")
    return ",".join(parts)


# ------------------------------------------------------------ shot rendering


def render_shot(
    shot: dict, index: int, total: int, *, work: str, base_dir: str, brand: dict,
    w: int, h: int, fps: int, transition: str, preview: bool,
) -> dict:
    kind = shot["type"]
    out = os.path.join(work, f"shot{index:03d}.mp4")
    bg = brand["colors"]["background"]
    canvas_aspect = w / h

    inputs: list[str] = []
    chain: list[str] = []
    keeps_audio = False

    if kind == "color":
        duration = float(parse_time(shot["duration"]))
        color = shot.get("color", bg)
        inputs = ["-f", "lavfi", "-t", f"{duration:.3f}",
                  "-i", f"color=c={color}:s={w}x{h}:r={fps}"]
        chain.append("setsar=1")
    elif kind == "image":
        src = resolve(shot["src"], base_dir)
        duration = float(parse_time(shot["duration"]))
        info = probe(src)
        fit = fit_mode(shot, info.aspect, canvas_aspect)
        frames = max(2, int(round(duration * fps)))
        # Work at twice the canvas so the Ken Burns move resamples from real
        # detail instead of magnifying an already-downscaled frame.
        big_w, big_h = w * 2, h * 2
        inputs = ["-i", src]
        chain.append(geometry_chain(fit, big_w, big_h, bg))
        chain.append(zoompan_chain(shot, w, h, fps, frames))
        chain.append(f"trim=end_frame={frames}")
        duration = frames / fps
    else:  # video | library
        src = resolve(shot["src"], base_dir)
        info = probe(src)
        start = float(parse_time(shot.get("in", 0)))
        end = float(parse_time(shot["out"])) if shot.get("out") is not None else info.duration
        duration = max(0.05, end - start)
        fit = fit_mode(shot, info.aspect, canvas_aspect)
        speed = float(shot.get("speed", 1.0))
        # Seek before -i so ffmpeg jumps rather than decodes from the top; with
        # a re-encode following, the cut is still frame-accurate.
        inputs = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src]
        chain.append(geometry_chain(fit, w, h, bg))
        chain.append(f"fps={fps}")
        if speed != 1.0:
            chain.append(f"setpts=PTS/{speed}")
            duration = duration / speed
        wants_audio = shot.get("audio", "auto")
        if wants_audio == "auto":
            wants_audio = kind == "library"
        keeps_audio = bool(wants_audio) and info.has_audio and not shot.get("mute")

    text = text_chain(shot, brand, w, h, duration, work, index)
    if text:
        chain.append(text)
    dip = dip_chain(index, total, duration, transition, bg)
    if dip:
        chain.append(dip)
    chain.append("format=yuv420p")

    filtergraph = ",".join(c for c in chain if c)
    # A fit=blur chain contains its own labels, so it has to be spliced in as a
    # filter_complex graph rather than a simple comma-joined -vf.
    complex_graph = "[0:v]" + filtergraph + "[vout]"

    args = list(inputs)
    args += ["-filter_complex", complex_graph, "-map", "[vout]"]

    if keeps_audio:
        src_speed = float(shot.get("speed", 1.0))
        gain = float(shot.get("gain_db", 0.0))
        afilters = [f"aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if src_speed != 1.0:
            afilters.insert(0, f"atempo={min(max(src_speed, 0.5), 2.0)}")
        if gain:
            afilters.append(f"volume={gain}dB")
        afilters.append(f"apad=whole_dur={duration:.3f}")
        afilters.append(f"atrim=duration={duration:.3f}")
        args += ["-filter:a", ",".join(afilters), "-map", "0:a:0"]
    else:
        # Prepending the silent input makes it stream 0 and shifts the picture
        # to stream 1, so the graph has to be relabelled to match.
        args = ["-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}"] + args
        args += ["-map", "0:a"]
        args[args.index("-filter_complex") + 1] = "[1:v]" + filtergraph + "[vout]"

    crf = "30" if preview else "18"
    preset = "ultrafast" if preview else "medium"
    args += [
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
        "-r", str(fps), "-video_track_timescale", "90000",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-shortest", out,
    ]
    ffmpeg(args)

    actual = probe(out)
    return {
        "path": out,
        "duration": actual.duration or duration,
        "speech": keeps_audio,
        "shot": shot,
        "index": index,
    }


# ----------------------------------------------------------------- captions


def catalog_lookup(base_dir: str, library: str | None) -> dict:
    """Map a media path to its transcript slug, so library shots can be
    captioned from the transcript that already exists."""
    roots = []
    if library:
        roots.append(os.path.expanduser(library))
    roots += [os.environ.get("PRODUCT_VIDEO_LIBRARY", ""), os.path.join(base_dir, "library"), "library"]
    for root in roots:
        if not root:
            continue
        path = os.path.join(os.path.expanduser(root), "catalog.json")
        if os.path.exists(path):
            with open(path) as fh:
                catalog = json.load(fh)
            return {
                os.path.realpath(meta["media"]): (slug, os.path.expanduser(root))
                for slug, meta in catalog.get("sources", {}).items()
            }
    return {}


def words_from_library(root: str, slug: str, start: float, end: float) -> list[tuple[str, float, float]]:
    path = os.path.join(root, "transcripts", f"{slug}.words.json")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        segments = json.load(fh)["segments"]
    words: list[tuple[str, float, float]] = []
    for seg in segments:
        if seg["end"] < start or seg["start"] > end:
            continue
        for text, w_start, w_end in seg["words"]:
            if w_end >= start and w_start <= end:
                words.append((text, max(w_start, start), min(w_end, end)))
    return words


def transcribe_audio(path: str, work: str, model: str) -> list[tuple[str, float, float]]:
    """Word timings for audio we have no transcript for (TTS, or a shot flagged
    transcribe:true)."""
    require("whisper-cli")
    if not os.path.exists(model):
        print(f"warning: whisper model missing at {model}; skipping captions for {os.path.basename(path)}",
              file=sys.stderr)
        return []
    wav = os.path.join(work, "caption-src.wav")
    ffmpeg(["-i", path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
    base = os.path.join(work, "caption-src")
    run(["whisper-cli", "-m", model, "-f", wav, "-l", "en",
         "-ml", "1", "-sow", "-oj", "-of", base, "-np"])
    with open(base + ".json") as fh:
        data = json.load(fh)
    words = []
    for item in data.get("transcription", []):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        offsets = item.get("offsets") or {}
        words.append((text, float(offsets.get("from", 0)) / 1000, float(offsets.get("to", 0)) / 1000))
    return words


def group_cues(words: list[tuple[str, float, float]], max_chars: int,
               max_seconds: float = 3.2) -> list[tuple[float, float, str]]:
    """Two short lines at a time reads far better on a phone than one long one.

    A wide canvas fits many more characters per line, so a character budget on
    its own would leave a single cue parked on screen for eight seconds. The
    time cap keeps captions moving with the speech at every aspect ratio.
    """
    cues: list[tuple[float, float, str]] = []
    chunk: list[tuple[str, float, float]] = []
    for word in words:
        gap = word[1] - chunk[-1][2] if chunk else 0.0
        too_wide = sum(len(c[0]) + 1 for c in chunk) + len(word[0]) > max_chars
        too_long = bool(chunk) and (word[2] - chunk[0][1]) > max_seconds
        if chunk and (too_wide or too_long or gap > 0.7):
            cues.append((chunk[0][1], chunk[-1][2], " ".join(c[0] for c in chunk)))
            chunk = []
        chunk.append(word)
    if chunk:
        cues.append((chunk[0][1], chunk[-1][2], " ".join(c[0] for c in chunk)))
    return [(s, max(e, s + 0.5), t) for s, e, t in cues]


def write_ass(path: str, cues: list[tuple[float, float, str]], brand: dict, w: int, h: int) -> None:
    conf = brand["caption"]
    size = int(h * float(conf["font_size_ratio"]))
    margin = int(h * float(conf["margin_ratio"]))
    font_name = conf.get("font_family") or "Arial"
    bold = -1 if conf.get("bold", True) else 0
    primary = ass_color(brand["colors"]["text"])
    outline_c = ass_color(brand["colors"]["text_shadow"])
    align = {"bottom": 2, "center": 5, "top": 8}.get(conf.get("position", "bottom"), 2)

    # WrapStyle 0 lets libass wrap, and libass actually measures the font
    # instead of estimating from a character count the way drawtext forces us
    # to. Within the margins below, captions cannot overflow the frame.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_name},{size},{primary},{primary},{outline_c},&H80000000,{bold},0,0,0,100,100,0,0,1,{conf['outline']},2,{align},{int(w*0.07)},{int(w*0.07)},{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w") as fh:
        fh.write(header)
        for start, end, text in cues:
            body = text.upper() if conf.get("uppercase") else text
            body = body.replace("\n", " ").strip()
            fh.write(f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},Caption,,0,0,0,,{body}\n")


def write_srt(path: str, cues: list[tuple[float, float, str]]) -> None:
    with open(path, "w") as fh:
        for i, (start, end, text) in enumerate(cues, 1):
            fh.write(f"{i}\n{fmt_timecode(start)} --> {fmt_timecode(end)}\n{text}\n\n")


# -------------------------------------------------------------------- audio


def make_tts(spec: dict, work: str) -> str:
    """macOS 'say' narration. Free and offline, and good enough for a rough cut
    even though it will never pass for the real presenter."""
    if shutil.which("say") is None:
        raise ToolError("audio.mode is 'tts' but macOS 'say' is unavailable.")
    raw = os.path.join(work, "tts-raw.wav")
    wav = os.path.join(work, "tts.wav")
    # 'say' infers the container from --file-format, not the extension, and
    # rejects little-endian samples in an AIFF, hence the explicit pairing.
    cmd = ["say", "-o", raw, "--file-format=WAVE", "--data-format=LEI16@22050"]
    if spec.get("voice"):
        cmd += ["-v", str(spec["voice"])]
    if spec.get("rate"):
        cmd += ["-r", str(int(spec["rate"]))]
    cmd.append(spec["text"])
    run(cmd)
    ffmpeg(["-i", raw, "-ac", "2", "-ar", str(AUDIO_RATE), wav])
    return wav


def duck_expression(intervals: list[tuple[float, float]], duck_db: float) -> str:
    if not intervals:
        return ""
    linear = 10 ** (duck_db / 20.0)
    terms = "+".join(
        f"between(t,{max(0.0, a - DUCK_PAD):.3f},{b + DUCK_PAD:.3f})" for a, b in intervals
    )
    return f"volume='if(gt({terms},0),{linear:.4f},1)':eval=frame"


# ------------------------------------------------------------------- render


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl", help="path to the EDL json")
    ap.add_argument("-o", "--output", help="output video path")
    ap.add_argument("--canvas", help="override the EDL canvas, e.g. 9x16, 1x1, 16x9, 1080x1350")
    ap.add_argument("--brand", help="brand json (overrides the EDL's brand)")
    ap.add_argument("--library", help="library root, for captioning library shots")
    ap.add_argument("--preview", action="store_true", help="fast half-size draft")
    ap.add_argument("--check", action="store_true", help="validate the EDL and report the timeline, render nothing")
    ap.add_argument("--captions", choices=["burn", "srt", "none"], help="override captions.mode")
    ap.add_argument("--keep-work", action="store_true", help="keep the per-shot intermediates for inspection")
    ap.add_argument("--model", default=os.path.expanduser("~/whisper-models/ggml-large-v3-turbo.bin"))
    args = ap.parse_args()

    edl_path = os.path.abspath(args.edl)
    base_dir = os.path.dirname(edl_path)
    edl = load_edl(edl_path)

    problems = validate(edl, base_dir)
    if problems:
        print("EDL problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    w, h = canvas_size(args.canvas or edl.get("canvas", "9x16"))
    fps = int(edl.get("fps", 30))
    brand = load_brand(args.brand or edl.get("brand"))
    transition = (edl.get("transition") or "cut").lower()
    audio_spec = edl.get("audio") or {"mode": "silence"}
    caption_mode = args.captions or (edl.get("captions") or {}).get("mode", "burn")

    if args.check:
        offset = 0.0
        print(f"canvas {w}x{h} @ {fps}fps   transition {transition}   audio {audio_spec.get('mode','silence')}   captions {caption_mode}")
        for n, shot in enumerate(edl["shots"], 1):
            if shot["type"] == "image" or shot["type"] == "color":
                dur = float(parse_time(shot["duration"]))
            else:
                info = probe(resolve(shot["src"], base_dir))
                start = float(parse_time(shot.get("in", 0)))
                end = float(parse_time(shot["out"])) if shot.get("out") is not None else info.duration
                dur = (end - start) / float(shot.get("speed", 1.0))
            label = shot.get("src", shot.get("color", ""))
            print(f"  {n:>2}. {shot['type']:<8} {dur:>5.2f}s  @{offset:>6.2f}s  {os.path.basename(str(label))}")
            offset += dur
        print(f"\ntotal {offset:.2f}s")
        if not 20 <= offset <= 75:
            print(f"note: {offset:.1f}s is outside the usual 30-60s window for a short.")
        return 0

    if not args.output:
        ap.error("-o/--output is required unless --check is used")
    output = os.path.abspath(os.path.expanduser(args.output))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    work = tempfile.mkdtemp(prefix="pvs-render-")
    try:
        total = len(edl["shots"])
        rendered = []
        for i, shot in enumerate(edl["shots"]):
            print(f"[shot {i+1}/{total}] {shot['type']} ...", flush=True)
            rendered.append(render_shot(
                shot, i, total, work=work, base_dir=base_dir, brand=brand,
                w=w, h=h, fps=fps, transition=transition, preview=args.preview,
            ))

        offsets, cursor = [], 0.0
        for item in rendered:
            offsets.append(cursor)
            cursor += item["duration"]
        total_duration = cursor
        print(f"[timeline] {total_duration:.2f}s across {total} shots")

        # ---- captions -------------------------------------------------
        cues: list[tuple[float, float, str]] = []
        lookup = catalog_lookup(base_dir, args.library)
        # Budget a cue at two lines of type. Shorter than that and the
        # captions strobe; longer and they cover the product.
        cap_size = h * float(brand["caption"]["font_size_ratio"])
        chars_per_line = max(12, int((w * 0.86) / (cap_size * CHAR_WIDTH)))
        max_chars = chars_per_line * 2

        if caption_mode != "none":
            for item, offset in zip(rendered, offsets):
                shot = item["shot"]
                if not item["speech"]:
                    continue
                src = resolve(shot.get("src", ""), base_dir)
                start = float(parse_time(shot.get("in", 0)))
                words: list[tuple[str, float, float]] = []
                entry = lookup.get(os.path.realpath(src)) if src else None
                if entry:
                    slug, root = entry
                    end = start + item["duration"] * float(shot.get("speed", 1.0))
                    words = [(t, s - start, e - start) for t, s, e in words_from_library(root, slug, start, end)]
                elif shot.get("transcribe"):
                    words = transcribe_audio(item["path"], work, args.model)
                if words:
                    speed = float(shot.get("speed", 1.0))
                    cues += [
                        (offset + s / speed, offset + e / speed, t)
                        for s, e, t in group_cues(words, max_chars)
                    ]

        # ---- audio ----------------------------------------------------
        mode = audio_spec.get("mode", "silence")
        speech_intervals = [
            (offset, offset + item["duration"])
            for item, offset in zip(rendered, offsets) if item["speech"]
        ]

        extra_inputs: list[str] = []
        audio_labels: list[str] = []
        graph: list[str] = []

        concat_inputs = "".join(f"[{i}:v][{i}:a]" for i in range(total))
        graph.append(f"{concat_inputs}concat=n={total}:v=1:a=1[vcat][acat]")
        audio_labels.append("[acat]")
        next_index = total

        if mode == "tts":
            tts_wav = make_tts(audio_spec["tts"], work)
            extra_inputs += ["-i", tts_wav]
            tts_delay = float(audio_spec["tts"].get("start", 0.0))
            tts_chain = [f"aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
            if tts_delay > 0:
                tts_chain.append(f"adelay={int(tts_delay*1000)}|{int(tts_delay*1000)}")
            gain = float(audio_spec["tts"].get("gain_db", 0.0))
            if gain:
                tts_chain.append(f"volume={gain}dB")
            # Library clips are the real voice; let them win over the robot.
            duck = duck_expression(speech_intervals, float(audio_spec.get("duck_db", -22)))
            if duck:
                tts_chain.append(duck)
            graph.append(f"[{next_index}:a]" + ",".join(tts_chain) + "[tts]")
            audio_labels.append("[tts]")
            if caption_mode != "none" and audio_spec["tts"].get("captions", True):
                words = transcribe_audio(tts_wav, work, args.model)
                cues += [(s + tts_delay, e + tts_delay, t) for s, e, t in group_cues(words, max_chars)]
            next_index += 1

        if mode == "music" or audio_spec.get("music"):
            music = resolve(audio_spec["music"], base_dir)
            extra_inputs += ["-stream_loop", "-1", "-i", music]
            fade = float(audio_spec.get("fade_out", 1.2))
            music_chain = [
                f"aresample={AUDIO_RATE}",
                "aformat=sample_fmts=fltp:channel_layouts=stereo",
                f"atrim=duration={total_duration:.3f}",
                "asetpts=N/SR/TB",
                f"volume={float(audio_spec.get('music_gain_db', -8)):g}dB",
            ]
            duck = duck_expression(speech_intervals, float(audio_spec.get("duck_db", -14)))
            if duck:
                music_chain.append(duck)
            music_chain.append(f"afade=t=in:st=0:d=0.6")
            if total_duration > fade:
                music_chain.append(f"afade=t=out:st={total_duration - fade:.3f}:d={fade:.3f}")
            graph.append(f"[{next_index}:a]" + ",".join(music_chain) + "[music]")
            audio_labels.append("[music]")
            next_index += 1

        target_lufs = float(brand.get("loudness_target_lufs", -14.0))
        if len(audio_labels) > 1:
            graph.append(
                "".join(audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0[amixed]"
            )
            audio_src = "[amixed]"
        else:
            audio_src = audio_labels[0]
        graph.append(f"{audio_src}loudnorm=I={target_lufs}:TP=-1.5:LRA=11,alimiter=limit=0.97[aout]")

        # ---- video tail: burn captions --------------------------------
        video_src = "[vcat]"
        srt_path = os.path.splitext(output)[0] + ".srt"
        if cues:
            cues.sort(key=lambda c: c[0])
            write_srt(srt_path, cues)
            if caption_mode == "burn":
                ass_path = os.path.join(work, "captions.ass")
                write_ass(ass_path, cues, brand, w, h)
                graph.append(f"[vcat]ass='{escape_filter_path(ass_path)}'[vburn]")
                video_src = "[vburn]"
        elif caption_mode != "none":
            print("note: no captions written - no shot carried a transcript "
                  "(library shots caption automatically; add \"transcribe\": true to caption another clip)")

        inputs: list[str] = []
        for item in rendered:
            inputs += ["-i", item["path"]]
        inputs += extra_inputs

        encoder = ["-c:v", "libx264", "-preset", "ultrafast" if args.preview else "slow",
                   "-crf", "30" if args.preview else "19", "-profile:v", "high", "-level", "4.1"]
        scale_tail = ""
        if args.preview:
            pw, ph = (w // 2) // 2 * 2, (h // 2) // 2 * 2
            scale_tail = f",scale={pw}:{ph}"
        graph.append(f"{video_src}format=yuv420p{scale_tail}[vout]")

        print("[final] mixing audio, encoding ...", flush=True)
        ffmpeg(inputs + [
            "-filter_complex", ";".join(graph),
            "-map", "[vout]", "-map", "[aout]",
            *encoder,
            "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2",
            "-movflags", "+faststart", "-r", str(fps),
            output,
        ])

        final = probe(output)
        size_mb = os.path.getsize(output) / 1e6
        print(f"\n{output}")
        print(f"  {final.width}x{final.height}  {final.duration:.2f}s  {size_mb:.1f} MB  {final.fps:g}fps")
        if cues:
            print(f"  captions: {len(cues)} cues ({'burned in + ' if caption_mode == 'burn' else ''}{srt_path})")
        if not 25 <= final.duration <= 65:
            print(f"  note: {final.duration:.1f}s falls outside the 30-60s target.")
        return 0
    finally:
        if args.keep_work:
            print(f"work dir kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
