"""Shared helpers for the product-video-studio scripts.

Everything here exists for one reason: the Homebrew tools live in
/opt/homebrew/bin, which is added to PATH by ~/.zprofile. Login shells read
that file; the non-login shells that run these scripts do not. So any script
that shells out to ffmpeg has to put the directory back on PATH itself,
otherwise it fails with a confusing "command not found".
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

BREW_BIN = "/opt/homebrew/bin"
WHISPER_MODEL = os.path.expanduser("~/whisper-models/ggml-large-v3-turbo.bin")

CANVASES = {
    "9x16": (1080, 1920),
    "1x1": (1080, 1080),
    "16x9": (1920, 1080),
    "4x5": (1080, 1350),
}


def ensure_path() -> None:
    """Prepend the Homebrew bin dir to PATH if it is not already there."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if BREW_BIN not in parts:
        os.environ["PATH"] = os.pathsep.join([BREW_BIN] + parts)


ensure_path()


class ToolError(RuntimeError):
    pass


def require(*tools: str) -> None:
    """Fail early with an actionable message rather than deep inside a pipeline."""
    import shutil

    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise ToolError(
            "Missing required tool(s): "
            + ", ".join(missing)
            + f"\nExpected them in {BREW_BIN}. Install with:\n"
            "  brew tap homebrew-ffmpeg/ffmpeg\n"
            "  brew install homebrew-ffmpeg/ffmpeg/ffmpeg\n"
            "  brew install whisper-cpp auto-editor yt-dlp imagemagick"
        )


def run(cmd: list[str], *, quiet: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, surfacing stderr when it fails.

    ffmpeg writes its banner and progress to stderr even on success, so the
    output is only worth showing when something actually went wrong.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-30:])
        raise ToolError(
            f"Command failed ({proc.returncode}):\n  {shlex.join(cmd)}\n--- stderr (last 30 lines) ---\n{tail}"
        )
    if not quiet and proc.stdout:
        print(proc.stdout)
    return proc


def ffmpeg(args: list[str], *, overwrite: bool = True) -> subprocess.CompletedProcess:
    require("ffmpeg")
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if overwrite:
        base.append("-y")
    return run(base + args)


# ---------------------------------------------------------------- probing


@dataclass
class MediaInfo:
    path: str
    kind: str  # "video" | "audio" | "image"
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    sample_rate: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        d["aspect"] = round(self.aspect, 4)
        return d


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff", ".bmp", ".gif"}


def _rational(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe(path: str) -> MediaInfo:
    require("ffprobe")
    proc = run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ]
    )
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    ext = os.path.splitext(path)[1].lower()
    duration = _rational(fmt.get("duration"))

    if video is not None and (ext in IMAGE_EXTS or duration == 0.0 and audio is None):
        kind = "image"
    elif video is not None:
        kind = "video"
    else:
        kind = "audio"

    info = MediaInfo(path=path, kind=kind, duration=duration, raw=data)
    if video is not None:
        info.width = int(video.get("width") or 0)
        info.height = int(video.get("height") or 0)
        info.fps = round(_rational(video.get("avg_frame_rate") or video.get("r_frame_rate")), 3)
        if kind == "video" and not info.duration:
            info.duration = _rational(video.get("duration"))
    if audio is not None:
        info.has_audio = True
        info.sample_rate = int(audio.get("sample_rate") or 0)
        if kind == "audio" and not info.duration:
            info.duration = _rational(audio.get("duration"))
    return info


def loudness(path: str) -> dict:
    """Measure integrated loudness and true peak with the loudnorm filter."""
    require("ffmpeg")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", path,
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    err = proc.stderr or ""
    start = err.rfind("{")
    end = err.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(err[start : end + 1])
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- canvas


def canvas_size(spec) -> tuple[int, int]:
    """Accept '9x16', '1080x1920', or {'w':..,'h':..} and return (w, h)."""
    if isinstance(spec, dict):
        return int(spec["w"]), int(spec["h"])
    key = str(spec).strip().lower().replace(":", "x")
    if key in CANVASES:
        return CANVASES[key]
    if "x" in key:
        w, _, h = key.partition("x")
        try:
            return int(w), int(h)
        except ValueError:
            pass
    raise ValueError(
        f"Unrecognised canvas {spec!r}. Use one of {sorted(CANVASES)} or an explicit WxH like 1080x1350."
    )


# ---------------------------------------------------------------- timecode


def parse_time(value) -> float:
    """Seconds from a float, '12.5', '1:23', '01:02:03.250' or '90s'."""
    if value is None:
        raise ValueError("expected a time, got None")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().rstrip("s") if str(value).strip().lower().endswith("s") and ":" not in str(value) else str(value).strip()
    if ":" in text:
        bits = [float(b) for b in text.split(":")]
        total = 0.0
        for b in bits:
            total = total * 60 + b
        return total
    return float(text)


def fmt_timecode(seconds: float, *, sep: str = ",") -> str:
    """SRT/ASS style timecode. sep=',' for SRT, '.' for VTT."""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    ms = int(round((secs - int(secs)) * 1000))
    if ms == 1000:  # rounding can tip a second over
        secs, ms = int(secs) + 1, 0
    return f"{int(hours):02d}:{int(mins):02d}:{int(secs):02d}{sep}{ms:03d}"


def fmt_ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    cs = int(round((secs - int(secs)) * 100))
    if cs == 100:
        secs, cs = int(secs) + 1, 0
    return f"{int(hours):d}:{int(mins):02d}:{int(secs):02d}.{cs:02d}"


def fmt_clock(seconds: float) -> str:
    """Compact human timecode for transcripts and reports: 1:02:03.4"""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{int(hours)}:{int(mins):02d}:{secs:04.1f}"
    return f"{int(mins)}:{secs:04.1f}"


# ---------------------------------------------------------------- brand


DEFAULT_BRAND = {
    "name": "default",
    "font": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "font_regular": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "colors": {
        "text": "white",
        "text_shadow": "black@0.65",
        "accent": "#E8B33B",
        "background": "#101417",
        "panel": "black@0.55",
    },
    "caption": {
        # libass resolves fonts by family name, not by file, so this is the
        # installed family (e.g. "Arial", "Avenir Next") rather than a path.
        "font_family": "Arial",
        "bold": True,
        "font_size_ratio": 0.042,
        "position": "bottom",
        "margin_ratio": 0.13,
        "outline": 3,
        "uppercase": False,
    },
    "headline": {
        # A fraction of the canvas height, so the same brand file gives
        # sensible type on a 9:16 phone cut and a 16:9 web cut alike.
        "font_size_ratio": 0.052,
        "position": "top",
        "margin_ratio": 0.10,
        "max_chars_per_line": None,
    },
    "logo": None,
    "logo_width_ratio": 0.22,
    "safe_area_ratio": 0.06,
    "loudness_target_lufs": -14.0,
}


def load_brand(path: str | None) -> dict:
    """Merge a brand file over the defaults, so a partial file still works."""
    brand = json.loads(json.dumps(DEFAULT_BRAND))  # deep copy
    if not path:
        return brand
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        raise ToolError(f"Brand file not found: {path}")
    with open(expanded) as fh:
        override = json.load(fh)

    def merge(base: dict, extra: dict) -> dict:
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value
        return base

    merge(brand, override)
    for key in ("font", "font_regular", "logo"):
        if brand.get(key):
            brand[key] = os.path.expanduser(brand[key])
    return brand


def ass_color(value: str) -> str:
    """Convert '#RRGGBB', 'white', or 'black@0.5' into ASS &HAABBGGRR."""
    named = {
        "white": "#FFFFFF", "black": "#000000", "red": "#FF0000",
        "green": "#00FF00", "blue": "#0000FF", "yellow": "#FFFF00",
        "orange": "#FF8000", "grey": "#808080", "gray": "#808080",
    }
    text = str(value).strip()
    alpha = 0.0  # ASS alpha is inverted: 00 = opaque
    if "@" in text:
        text, _, a = text.partition("@")
        try:
            alpha = 1.0 - float(a)
        except ValueError:
            alpha = 0.0
    text = named.get(text.lower(), text).lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        text = "FFFFFF"
    rr, gg, bb = text[0:2], text[2:4], text[4:6]
    aa = f"{int(round(max(0.0, min(1.0, alpha)) * 255)):02X}"
    return f"&H{aa}{bb}{gg}{rr}"


def escape_drawtext(text: str) -> str:
    """Escape a literal string for ffmpeg's drawtext text= option.

    drawtext is parsed twice (filtergraph, then the filter), which is why
    colons, backslashes and quotes all need handling.
    """
    out = text.replace("\\", "\\\\\\\\")
    for ch in (":", "'", "%", "[", "]", ",", ";"):
        out = out.replace(ch, "\\" + ch)
    return out.replace("\n", "\\n")


def escape_filter_path(path: str) -> str:
    """Escape a file path used inside a filtergraph option (ass=, subtitles=)."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def wrap_text(text: str, max_chars: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def slugify(text: str, *, maxlen: int = 60) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum():
            keep.append(ch)
        elif ch in " -_./":
            keep.append("-")
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:maxlen] or "untitled"


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)
