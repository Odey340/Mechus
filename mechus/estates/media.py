"""Media ingest: hashing, metadata, keyframes, audio, perceptual hashes.

ffmpeg comes from the ``imageio-ffmpeg`` wheel, so no system install is needed.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

try:  # iPhone photos arrive as HEIC
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".3gp"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".ogg", ".opus"}


def ffmpeg_exe() -> str:
    explicit = os.getenv("MECHUS_FFMPEG")
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg not available (pip install imageio-ffmpeg)")
        return found


def media_kind(filename: str, mime: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    mime = (mime or "").lower()
    if ext in VIDEO_EXT or mime.startswith("video/"):
        return "video"
    if ext in PHOTO_EXT or mime.startswith("image/"):
        return "photo"
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "audio"
    return ""


def sniff_extension(data_head: bytes, fallback: str = "") -> str:
    """Pick an extension from magic bytes (inbound iMessage MIME types are unreliable)."""
    if data_head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data_head.startswith(b"\x89PNG"):
        return ".png"
    if data_head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return ".heic"
    if data_head[4:8] == b"ftyp":
        return ".mov" if b"qt  " in data_head[8:16] else ".mp4"
    if data_head.startswith(b"RIFF") and data_head[8:12] == b"WAVE":
        return ".wav"
    if data_head.startswith(b"caff"):
        return ".caf"
    if data_head.startswith(b"RIFF") and data_head[8:12] == b"WEBP":
        return ".webp"
    return fallback or mimetypes.guess_extension("application/octet-stream") or ".bin"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Probe:
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    has_video: bool = False
    created_at: str = ""


def probe(path: Path) -> Probe:
    """Parse `ffmpeg -i` stderr (imageio-ffmpeg ships ffmpeg but not ffprobe)."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60)
    text = proc.stderr or ""
    out = Probe()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        out.duration_s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if m:
        out.has_video = True
        out.width, out.height = int(m.group(1)), int(m.group(2))
    out.has_audio = bool(re.search(r"Stream #.*Audio:", text))
    m = re.search(r"creation_time\s*:\s*(\S+)", text)
    if m:
        out.created_at = m.group(1)
    return out


def photo_metadata(path: Path) -> dict[str, object]:
    meta: dict[str, object] = {}
    try:
        with Image.open(path) as img:
            meta["width"], meta["height"] = img.size
            exif = img.getexif()
            raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
            if not raw:
                try:
                    raw = exif.get_ifd(0x8769).get(36867)
                except Exception:
                    raw = None
            if raw:
                try:
                    meta["captured_at"] = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").isoformat()
                except ValueError:
                    pass
    except Exception as exc:
        log.info("photo metadata unavailable for %s: %s", path.name, exc)
    return meta


def normalize_photo(src: Path, dest: Path, *, max_side: int = 1600) -> tuple[int, int]:
    """Orientation-corrected RGB JPEG copy (EXIF stripped) for analysis and reports."""
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_side, max_side))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=86, optimize=True)
        return img.size


def dhash(img: Image.Image, size: int = 8) -> int:
    small = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    px = np.asarray(small, dtype=np.int16)
    bits = (px[:, 1:] > px[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def sharpness(img: Image.Image) -> float:
    """Variance of a Laplacian (higher = sharper)."""
    gray = np.asarray(img.convert("L").resize((320, int(320 * img.height / max(1, img.width)) or 1)), dtype=np.float32)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    lap = gray[1:-1, 1:-1] * -4 + gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
    return float(lap.var())


def brightness(img: Image.Image) -> float:
    return float(np.asarray(img.convert("L").resize((64, 64)), dtype=np.float32).mean())


@dataclass
class Keyframe:
    path: Path
    t: float
    phash: int
    sharpness: float


def extract_keyframes(
    video: Path,
    out_dir: Path,
    *,
    duration_s: float | None = None,
    max_frames: int = 48,
    window_s: float = 1.5,
    min_distance: int = 7,
) -> list[Keyframe]:
    """Sample candidate frames, keep the sharpest per window, drop near-duplicates and dark/blurry frames."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = duration_s or 60.0
    fps = max(0.25, min(2.0, (max_frames * 3) / max(duration, 1.0)))
    with tempfile.TemporaryDirectory(prefix="mechus-frames-") as tmp:
        cmd = [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-vf", f"fps={fps:.3f},scale='min(1280,iw)':-2", "-q:v", "3", os.path.join(tmp, "c%05d.jpg"),
        ]
        subprocess.run(cmd, check=True, timeout=max(120, int(duration * 3)))
        candidates = sorted(Path(tmp).glob("c*.jpg"))
        scored: list[tuple[float, Path, float, int, float]] = []
        for idx, path in enumerate(candidates):
            t = idx / fps
            with Image.open(path) as img:
                if brightness(img) < 18:  # too dark to judge anything
                    continue
                scored.append((t, path, sharpness(img), dhash(img), brightness(img)))
        if not scored:
            return []
        median_sharp = float(np.median([s[2] for s in scored]))
        # Best frame per time window.
        windows: dict[int, tuple[float, Path, float, int, float]] = {}
        for item in scored:
            if item[2] < median_sharp * 0.35:  # motion blur
                continue
            key = int(item[0] // window_s)
            if key not in windows or item[2] > windows[key][2]:
                windows[key] = item
        picked: list[Keyframe] = []
        for _, (t, path, sharp, ph, _b) in sorted(windows.items()):
            if picked and hamming(ph, picked[-1].phash) < min_distance:
                if sharp > picked[-1].sharpness:  # same view: keep the sharper one
                    picked[-1] = Keyframe(path, t, ph, sharp)
                continue
            picked.append(Keyframe(path, t, ph, sharp))
        if len(picked) > max_frames:  # even thinning
            step = len(picked) / max_frames
            picked = [picked[int(i * step)] for i in range(max_frames)]
        final: list[Keyframe] = []
        for i, kf in enumerate(picked):
            dest = out_dir / f"{video.stem}-{i:03d}.jpg"
            shutil.copyfile(kf.path, dest)
            final.append(Keyframe(dest, round(kf.t, 2), kf.phash, kf.sharpness))
        return final


def extract_audio(src: Path, dest: Path, *, sample_rate: int = 16000) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
         "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, timeout=600,
    )
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 1000


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
