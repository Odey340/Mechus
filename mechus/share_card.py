"""
Generate Open Graph-style share card images (PNG) for meeting recap links.

Requires Pillow (PIL). Used by GET /share/<meeting_id>/card.png.
"""

from __future__ import annotations

import io
import logging
import os
import re
import textwrap
import time
from typing import Any

log = logging.getLogger(__name__)

# Standard OG image size (Facebook/Slack/LinkedIn)
CARD_W, CARD_H = 1200, 630
_BG = (26, 26, 46)
_ACCENT = (99, 102, 241)
_TEXT = (245, 245, 250)
_MUTED = (163, 163, 184)


def _try_load_font(size: int):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _wrap_lines(text: str, width: int) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    return textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False)


def build_share_context(
    meeting_id: str,
    *,
    final_summary: dict[str, Any] | None,
    session_meeting_link: str | None,
    session_started_at: float | None,
    session_status: str | None,
    worker_status: dict[str, Any],
) -> dict[str, Any]:
    """Collect title, time line, and detail bullets for HTML + PNG."""
    title = "Mechus meeting"
    when_line = ""
    detail_lines: list[str] = []
    badge = "Recap"

    fs = final_summary or {}
    ex = (fs.get("executive_summary") or "").strip()
    ga = fs.get("generated_at")

    if ex:
        one_line = ex.split("\n")[0].strip()
        title = one_line[:90] + ("…" if len(one_line) > 90 else "")
    if isinstance(ga, (int, float)):
        when_line = time.strftime("Summary · %b %d, %Y · %H:%M UTC", time.gmtime(ga))

    actions = fs.get("action_items") or []
    decisions = fs.get("decisions") or []
    if isinstance(actions, list) and actions:
        detail_lines.append(f"{len(actions)} action item(s)")
    if isinstance(decisions, list) and decisions:
        detail_lines.append(f"{len(decisions)} decision(s) captured")

    link = (session_meeting_link or "").strip() or str(worker_status.get("meeting_link") or "").strip()
    if link and "zoom.us" in link.lower() and not ex:
        m = re.search(r"/j/(\d+)", link, re.I)
        if m:
            title = f"Zoom meeting {m.group(1)}"

    # Live session overrides
    if session_status and session_status in ("starting", "joining", "active"):
        badge = "Live"
        if session_started_at:
            when_line = time.strftime("Started · %b %d, %Y · %H:%M UTC", time.gmtime(session_started_at))
        if not ex:
            title = "Mechus is in this meeting"
        if link:
            detail_lines.insert(0, "Session in progress")

    ws_status = str(worker_status.get("status") or "")
    if ws_status == "stopped" and not ex:
        when_line = when_line or "Session ended"
        badge = "Ended"

    if not when_line:
        when_line = f"Meeting ID · {meeting_id[:12]}…" if len(meeting_id) > 12 else f"Meeting ID · {meeting_id}"

    return {
        "meeting_id": meeting_id,
        "title": title,
        "when_line": when_line,
        "detail_lines": detail_lines[:4],
        "badge": badge,
        "has_summary": bool(ex),
    }


def render_share_card_png(ctx: dict[str, Any]) -> bytes | None:
    """Render PNG bytes, or None if Pillow is unavailable."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("Pillow not installed; share card image disabled. pip install pillow")
        return None

    title = str(ctx.get("title") or "Mechus meeting")
    when_line = str(ctx.get("when_line") or "")
    details = ctx.get("detail_lines") or []
    badge = str(ctx.get("badge") or "Recap")

    img = Image.new("RGB", (CARD_W, CARD_H), _BG)
    draw = ImageDraw.Draw(img)

    font_title = _try_load_font(40)
    font_sub = _try_load_font(26)
    font_body = _try_load_font(24)
    font_badge = _try_load_font(22)

    # Accent bar
    draw.rectangle([0, 0, 8, CARD_H], fill=_ACCENT)

    # Badge
    bx, by = 48, 48
    draw.rounded_rectangle([bx, by, bx + 120, by + 42], radius=8, fill=_ACCENT)
    if font_badge:
        draw.text((bx + 16, by + 8), badge, fill=_TEXT, font=font_badge)
    else:
        draw.text((bx + 16, by + 12), badge, fill=_TEXT)

    y = 120
    margin = 56
    max_text_w = CARD_W - margin * 2

    # Title (wrapped)
    title_lines: list[str] = []
    if font_title:
        # approximate char width for wrap
        title_lines = _wrap_lines(title, 42)
    else:
        title_lines = _wrap_lines(title, 50)
    for line in title_lines[:3]:
        if font_title:
            draw.text((margin, y), line, fill=_TEXT, font=font_title)
            y += 48
        else:
            draw.text((margin, y), line, fill=_TEXT)
            y += 28
    y += 8

    if when_line:
        if font_sub:
            draw.text((margin, y), when_line, fill=_MUTED, font=font_sub)
            y += 40
        else:
            draw.text((margin, y), when_line, fill=_MUTED)
            y += 28

    y += 16
    for d in details:
        line = f"· {d}"
        wrapped = _wrap_lines(line, 70) if font_body else [line]
        for wl in wrapped[:2]:
            if font_body:
                draw.text((margin, y), wl, fill=_TEXT, font=font_body)
                y += 32
            else:
                draw.text((margin, y), wl, fill=_TEXT)
                y += 24
        if y > CARD_H - 80:
            break

    # Footer
    foot = "Mechus · AI meeting notes"
    if font_sub:
        draw.text((margin, CARD_H - 52), foot, fill=_MUTED, font=font_sub)
    else:
        draw.text((margin, CARD_H - 40), foot, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
