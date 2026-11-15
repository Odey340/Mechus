"""Evidence images: highlight a finding's box on its frame."""

from __future__ import annotations

from pathlib import Path

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=8)
def _font(size: int):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()

COLORS = {
    "new_damage": (214, 69, 51), "missing": (214, 69, 51), "cleaning": (201, 138, 30),
    "pre_existing": (45, 93, 138), "wear_and_tear": (110, 120, 128), "resolved": (85, 115, 52), "defect": (45, 93, 138),
}


def mark(src: Path, dest: Path, box: list[float] | None, category: str, *, label: str = "") -> Path:
    """Copy `src` to `dest` with a rounded highlight around `box` (normalized)."""
    with Image.open(src) as img:
        img = img.convert("RGB")
        if box:
            w, h = img.size
            color = COLORS.get(category, (45, 93, 138))
            x0, y0, x1, y1 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
            pad = max(6, int(min(w, h) * 0.012))
            x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(w - 1, x1 + pad), min(h - 1, y1 + pad)
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(overlay)
            # Dim everything but the region so the eye lands on the issue.
            d.rectangle([0, 0, w, h], fill=(0, 0, 0, 70))
            d.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 0))
            img = Image.alpha_composite(img.convert("RGBA"), overlay)  # box area stays undimmed
            d = ImageDraw.Draw(img)
            stroke = max(3, int(min(w, h) * 0.006))
            d.rounded_rectangle([x0, y0, x1, y1], radius=stroke * 3, outline=color + (255,), width=stroke)
            if label:
                fsize = max(14, int(min(w, h) * 0.026))
                font = _font(fsize)
                tw = d.textlength(label, font=font)
                lh = fsize + 10
                ty = y0 - lh - 4 if y0 > lh + 6 else y1 + 4
                d.rounded_rectangle([x0, ty, x0 + tw + 16, ty + lh], radius=7, fill=color + (255,))
                d.text((x0 + 8, ty + 4), label, fill=(255, 255, 255, 255), font=font)
            img = img.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=86)
    return dest
