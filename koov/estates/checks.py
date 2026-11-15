"""The landlord's report for a live walkthrough call: how it went, with photos.

A walkthrough is a FaceTime call where Mechus guides the tenant room by room
(``koov/inspection.py``). The call worker saves photos under
``<notes>/checks/<meeting_id>/`` and keeps ``worker-status-<meeting_id>.json`` current.
This module turns that into a printable page at ``/checks/<meeting_id>`` and tells the
landlord when the call is over.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from koov.brand import ASSISTANT_NAME

log = logging.getLogger(__name__)

_ID = re.compile(r"[\w.-]{1,80}")
_PHOTO = re.compile(r"\d{3}\.jpg")

RESULT_TEXT = {
    "passed": ("Passed", "good"),
    "needs_review": ("Needs review", "bad"),
    "incomplete": ("Incomplete", "warn"),
    "no_answer": ("Call did not connect", "bad"),
    "in_progress": ("In progress", "warn"),
}


def photo_path(meeting_id: str, name: str) -> Path | None:
    """Path of a saved report photo, or None for anything that is not one."""
    from koov.notes_paths import resolve_notes_dir

    if not _ID.fullmatch(meeting_id or "") or not _PHOTO.fullmatch(name or ""):
        return None
    path = Path(resolve_notes_dir()) / "checks" / meeting_id / name
    return path if path.is_file() else None


def counts(check: dict[str, Any]) -> dict[str, int]:
    items = [i for room in check.get("rooms") or [] for i in room.get("items") or []]
    return {
        "areas": len(items),
        "ok": sum(1 for i in items if i.get("status") == "ok"),
        "issues": len(check.get("findings") or []),
        "not_shown": sum(1 for i in items if i.get("status") == "not_shown"),
        "not_reached": sum(1 for i in items if i.get("status") == "pending"),
    }


def headline(prop: dict[str, Any], check: dict[str, Any]) -> str:
    """One sentence for the landlord, e.g. "Joey passed the move-out check at Apt 4B."."""
    who = ((prop.get("tenant") or {}).get("name") or "The tenant").strip()
    unit = (prop.get("name") or "the unit").strip()
    kind = "move-out check" if check.get("mode") == "move_out" else "move-in tour"
    c = counts(check)
    result = check.get("result")
    if result == "passed":
        return f"{who} passed the {kind} at {unit}."
    if result == "needs_review":
        n = c["issues"]
        return f"{who}'s {kind} at {unit} needs your review: {n} issue{'s' if n != 1 else ''} found."
    if result == "no_answer":
        return f"The {kind} call to {who} at {unit} did not connect."
    if result == "in_progress":
        return f"{who}'s {kind} at {unit} is in progress."
    return f"{who}'s {kind} at {unit} is incomplete: some areas were not shown."


def summary_text(prop: dict[str, Any], check: dict[str, Any], url: str = "") -> str:
    c = counts(check)
    parts = [headline(prop, check)]
    if c["areas"]:
        parts.append(f"{c['ok']} of {c['areas']} areas checked out, {c['issues']} issue{'s' if c['issues'] != 1 else ''}"
                     + (f", {c['not_shown'] + c['not_reached']} not seen" if c["not_shown"] + c["not_reached"] else "") + ".")
    if url:
        parts.append(f"Report: {url}")
    return " ".join(parts)


def render(prop: dict[str, Any], check: dict[str, Any]) -> str:
    """The report page (self-contained HTML, printable)."""
    e = html.escape
    mid = check["meeting_id"]
    tenant = prop.get("tenant") or {}
    kind = "Move-out check" if check.get("mode") == "move_out" else "Move-in tour"
    label, tone = RESULT_TEXT.get(str(check.get("result")), ("Unknown", "warn"))
    when = datetime.fromtimestamp(float(check.get("at") or time.time())).strftime("%B %d, %Y at %I:%M %p").replace(" 0", " ")
    c = counts(check)

    def img(name: str, alt: str) -> str:
        if not name or photo_path(mid, name) is None:
            return '<div class="nophoto">No photo</div>'
        return f'<a href="/checks/{e(mid)}/photo/{e(name)}"><img loading="lazy" src="/checks/{e(mid)}/photo/{e(name)}" alt="{e(alt)}"></a>'

    findings = check.get("findings") or []
    if findings:
        cards = "".join(
            f'<article class="finding">{img(f.get("photo") or "", f.get("issue") or "issue")}<div>'
            f'<span class="sev {e(str(f.get("severity") or "low"))}">{e(str(f.get("severity") or "low"))}</span>'
            f'<h3>{e(str(f.get("issue") or ""))}</h3>'
            f'<p>{e(" · ".join(x for x in [str(f.get("room") or "").title(), str(f.get("item") or ""), str(f.get("surface") or "")] if x))}</p>'
            "</div></article>"
            for f in findings
        )
        issues_html = f'<section><h2>What {e(ASSISTANT_NAME)} found</h2><p class="hint">The red box marks the spot on each photo.</p>{cards}</section>'
    elif check.get("result") in ("passed", "incomplete"):
        issues_html = f"<section><h2>What {e(ASSISTANT_NAME)} found</h2><p>Nothing out of the ordinary in the areas that were shown.</p></section>"
    else:
        issues_html = ""

    status_text = {"ok": "Looks good", "issue": "Issue noted", "not_shown": "Not shown", "pending": "Not reached"}
    rooms_html = ""
    for room in check.get("rooms") or []:
        rows = "".join(
            f'<div class="stop">{img(i.get("photo") or "", i.get("item") or "")}'
            f'<div><b>{e(str(i.get("item") or "").capitalize())}</b>'
            f'<span class="st {e(str(i.get("status")))}">{e(status_text.get(str(i.get("status")), "Unknown"))}</span></div></div>'
            for i in room.get("items") or []
        )
        rooms_html += f'<h3 class="room">{e(str(room.get("room") or "").title())}</h3><div class="stops">{rows}</div>'
    if rooms_html:
        rooms_html = f"<section><h2>Room by room</h2>{rooms_html}</section>"

    error_html = f'<p class="error">{e(str(check.get("error")))}</p>' if check.get("error") else ""
    stats = "".join(
        f"<div><b>{n}</b><span>{e(t)}</span></div>"
        for n, t in ((c["ok"], "areas checked out"), (c["issues"], "issues found"), (c["not_shown"] + c["not_reached"], "areas not seen"))
    )
    refresh = '<meta http-equiv="refresh" content="5">' if check.get("result") == "in_progress" else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>{e(ASSISTANT_NAME)} · {e(kind)} report</title>
<link rel="icon" type="image/png" href="/static/img/favicon.png">
<style>
:root {{ --navy: #1a3c62; --ink: #12233a; --muted: #5a6779; --line: #dde3ec; --bg: #f4f6f9; --tint: #e8eef6;
  --good: #1f7a4d; --good-bg: #e3f4ea; --warn: #8a5a00; --warn-bg: #fcefd2; --bad: #a32a2a; --bad-bg: #fbe3e3; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 820px; margin: 0 auto; padding: 24px 16px 60px; }}
.brandbar {{ display: flex; align-items: center; padding-bottom: 16px; border-bottom: 2px solid var(--navy); }}
.brandbar img {{ height: 40px; width: auto; }}
.brandbar span {{ margin-left: auto; color: var(--muted); font-size: 13px; }}
.brandbar a.pdf {{ margin-left: 14px; padding: 7px 14px; border-radius: 8px; background: var(--navy); color: #fff; font-size: 13.5px; font-weight: 600; text-decoration: none; }}
@media print {{ .brandbar a.pdf {{ display: none; }} }}
h1 {{ font-size: 24px; color: var(--navy); margin: 22px 0 4px; }}
h2 {{ font-size: 17px; color: var(--navy); margin: 0 0 10px; }}
section {{ background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 18px; margin-top: 16px; }}
.meta {{ color: var(--muted); }}
.verdict {{ display: inline-block; margin-top: 12px; padding: 5px 14px; border-radius: 999px; font-weight: 700; }}
.verdict.good {{ background: var(--good-bg); color: var(--good); }} .verdict.warn {{ background: var(--warn-bg); color: var(--warn); }}
.verdict.bad {{ background: var(--bad-bg); color: var(--bad); }}
.headline {{ font-size: 17px; margin: 12px 0 0; }}
.stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 16px; }}
.stats div {{ background: var(--tint); border-radius: 10px; padding: 12px; }}
.stats b {{ display: block; font-size: 24px; color: var(--navy); }} .stats span {{ color: var(--muted); font-size: 13px; }}
.hint {{ color: var(--muted); font-size: 13px; margin: -4px 0 12px; }}
.finding {{ display: grid; grid-template-columns: 220px 1fr; gap: 14px; padding: 12px 0; border-top: 1px solid var(--line); align-items: start; }}
.finding h3 {{ margin: 6px 0 2px; font-size: 16px; }} .finding p {{ margin: 0; color: var(--muted); }}
img {{ width: 100%; border-radius: 8px; display: block; border: 1px solid var(--line); }}
.nophoto {{ display: grid; place-items: center; aspect-ratio: 4 / 3; border-radius: 8px; background: var(--tint); color: var(--muted); font-size: 12.5px; }}
.sev {{ display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 700; text-transform: uppercase; background: var(--warn-bg); color: var(--warn); }}
.sev.high {{ background: var(--bad-bg); color: var(--bad); }}
.room {{ margin: 16px 0 8px; font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
.stops {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; }}
.stop b {{ display: block; margin-top: 6px; }}
.st {{ font-size: 13px; font-weight: 600; }} .st.ok {{ color: var(--good); }} .st.issue {{ color: var(--bad); }}
.st.not_shown, .st.pending {{ color: var(--warn); }}
.error {{ background: var(--bad-bg); color: var(--bad); border-radius: 8px; padding: 10px 12px; }}
footer {{ color: var(--muted); font-size: 12.5px; margin-top: 18px; }}
@media (max-width: 560px) {{ .finding {{ grid-template-columns: 1fr; }} }}
@media print {{ body {{ background: #fff; }} section {{ break-inside: avoid; }} }}
</style></head><body><main>
<div class="brandbar"><img src="/static/img/mechus-logo.png" alt="{e(ASSISTANT_NAME)}"><span>{e(kind)} report</span>
<a class="pdf" href="/checks/{e(mid)}/report.pdf" download>Download PDF</a></div>
<h1>{e(str(prop.get("name") or "Unit"))}</h1>
<div class="meta">{e(" · ".join(x for x in [str(tenant.get("name") or ""), str(tenant.get("phone") or ""), when] if x))}</div>
<span class="verdict {tone}">{e(label)}</span>
<p class="headline">{e(headline(prop, check))}</p>
{error_html}
<div class="stats">{stats}</div>
{issues_html}
{rooms_html}
<footer>Recorded by {e(ASSISTANT_NAME)} during a live FaceTime walkthrough. Findings come from an AI reading camera
frames and can be wrong or incomplete; review the photos before making any decision about a deposit.</footer>
</main></body></html>"""


def pdf_filename(prop: dict[str, Any], check: dict[str, Any]) -> str:
    kind = "move-out" if check.get("mode") == "move_out" else "move-in"
    unit = re.sub(r"[^A-Za-z0-9]+", "-", str(prop.get("name") or "unit")).strip("-") or "unit"
    day = datetime.fromtimestamp(float(check.get("at") or time.time())).strftime("%Y-%m-%d")
    return f"mechus-{kind}-{unit}-{day}.pdf"


def render_pdf(prop: dict[str, Any], check: dict[str, Any]) -> bytes:
    """The same report as :func:`render`, as a PDF file (A4, photos included)."""
    from fpdf import FPDF

    navy, muted, line = (26, 60, 98), (90, 103, 121), (221, 227, 236)
    tones = {"good": ((227, 244, 234), (31, 122, 77)), "warn": ((252, 239, 210), (138, 90, 0)),
             "bad": ((251, 227, 227), (163, 42, 42))}

    def s(value: Any) -> str:  # the built-in PDF fonts are Latin-1 only
        text = str(value or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u2014", "-").replace("\u00b7", "-")
        return text.encode("latin-1", "replace").decode("latin-1")

    mid = check["meeting_id"]
    tenant = prop.get("tenant") or {}
    kind = "Move-out check" if check.get("mode") == "move_out" else "Move-in tour"
    label, tone = RESULT_TEXT.get(str(check.get("result")), ("Unknown", "warn"))
    when = datetime.fromtimestamp(float(check.get("at") or time.time())).strftime("%B %d, %Y at %I:%M %p")
    c = counts(check)

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(16, 14, 16)
    pdf.set_title(s(f"{ASSISTANT_NAME} {kind} report - {prop.get('name') or ''}"))
    pdf.add_page()
    width = pdf.w - 32

    def room_for(height: float) -> None:
        if pdf.get_y() + height > pdf.h - 16:
            pdf.add_page()

    def photo(name: str, x: float, y: float, w: float, h: float) -> None:
        path = photo_path(mid, name) if name else None
        if path is not None:
            try:
                pdf.image(str(path), x=x, y=y, w=w, h=h, keep_aspect_ratio=True)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("report pdf: could not place photo %s: %s", name, exc)
        pdf.set_fill_color(232, 238, 246)
        pdf.rect(x, y, w, h, style="F")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*muted)
        pdf.set_xy(x, y + h / 2 - 2)
        pdf.cell(w, 4, "No photo", align="C")

    def heading(text: str) -> None:
        room_for(16)
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(*navy)
        pdf.cell(0, 8, s(text), new_x="LMARGIN", new_y="NEXT")

    # header
    logo = Path(__file__).resolve().parent.parent / "static" / "img" / "mechus-logo.png"
    if logo.is_file():
        pdf.image(str(logo), x=16, y=12, h=11)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*muted)
    pdf.set_xy(16, 15)
    pdf.cell(width, 5, s(f"{kind} report"), align="R")
    pdf.set_draw_color(*navy)
    pdf.set_line_width(0.6)
    pdf.line(16, 26, pdf.w - 16, 26)
    pdf.set_xy(16, 31)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*navy)
    pdf.cell(0, 10, s(prop.get("name") or "Unit"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*muted)
    pdf.cell(0, 6, s(" - ".join(x for x in [tenant.get("name"), tenant.get("phone"), when] if x)), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    fill, ink = tones[tone]
    pdf.set_fill_color(*fill)
    pdf.set_text_color(*ink)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(pdf.get_string_width(s(label)) + 10, 8, s(label), fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_text_color(18, 35, 58)
    pdf.set_font("Helvetica", "", 12)
    pdf.multi_cell(0, 6, s(headline(prop, check)), new_x="LMARGIN", new_y="NEXT")
    if check.get("error"):
        pdf.set_text_color(163, 42, 42)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5, s(check["error"]), new_x="LMARGIN", new_y="NEXT")

    # counts
    pdf.ln(3)
    y = pdf.get_y()
    col = (width - 8) / 3
    for i, (n, text) in enumerate(((c["ok"], "areas checked out"), (c["issues"], "issues found"),
                                   (c["not_shown"] + c["not_reached"], "areas not seen"))):
        x = 16 + i * (col + 4)
        pdf.set_fill_color(232, 238, 246)
        pdf.rect(x, y, col, 18, style="F")
        pdf.set_xy(x + 4, y + 2)
        pdf.set_font("Helvetica", "B", 16)
        pdf.set_text_color(*navy)
        pdf.cell(col - 8, 8, str(n))
        pdf.set_xy(x + 4, y + 10)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*muted)
        pdf.cell(col - 8, 5, text)
    pdf.set_y(y + 22)

    # findings
    findings = check.get("findings") or []
    if findings or check.get("result") in ("passed", "incomplete"):
        heading(f"What {ASSISTANT_NAME} found")
    if findings:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*muted)
        pdf.cell(0, 5, "The red box marks the spot on each photo.", new_x="LMARGIN", new_y="NEXT")
        for f in findings:
            room_for(50)
            y = pdf.get_y() + 2
            photo(str(f.get("photo") or ""), 16, y, 60, 45)
            sev = str(f.get("severity") or "low")
            fill, ink = tones["bad" if sev == "high" else "warn"]
            pdf.set_xy(80, y)
            pdf.set_fill_color(*fill)
            pdf.set_text_color(*ink)
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(pdf.get_string_width(sev.upper()) + 6, 5, sev.upper(), fill=True, align="C")
            pdf.set_xy(80, y + 7)
            pdf.set_text_color(18, 35, 58)
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(width - 64, 6, s(f.get("issue")), new_x="LEFT", new_y="NEXT")
            pdf.set_x(80)
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*muted)
            where = " - ".join(x for x in [str(f.get("room") or "").title(), str(f.get("item") or ""), str(f.get("surface") or "")] if x)
            pdf.multi_cell(width - 64, 5, s(where), new_x="LMARGIN", new_y="NEXT")
            pdf.set_y(max(pdf.get_y(), y + 47))
    elif check.get("result") in ("passed", "incomplete"):
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(18, 35, 58)
        pdf.cell(0, 6, "Nothing out of the ordinary in the areas that were shown.", new_x="LMARGIN", new_y="NEXT")

    # room by room
    rooms = check.get("rooms") or []
    if rooms:
        heading("Room by room")
    status_text = {"ok": "Looks good", "issue": "Issue noted", "not_shown": "Not shown", "pending": "Not reached"}
    status_ink = {"ok": (31, 122, 77), "issue": (163, 42, 42)}
    cell_w, gap = (width - 8) / 3, 4
    for room in rooms:
        room_for(58)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*muted)
        pdf.cell(0, 7, s(str(room.get("room") or "").upper()), new_x="LMARGIN", new_y="NEXT")
        items = room.get("items") or []
        for start in range(0, len(items), 3):
            room_for(50)
            y = pdf.get_y()
            for i, item in enumerate(items[start:start + 3]):
                x = 16 + i * (cell_w + gap)
                photo(str(item.get("photo") or ""), x, y, cell_w, 36)
                pdf.set_xy(x, y + 37)
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(18, 35, 58)
                pdf.cell(cell_w, 5, s(str(item.get("item") or "").capitalize())[:38])
                pdf.set_xy(x, y + 41.5)
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(*status_ink.get(str(item.get("status")), (138, 90, 0)))
                pdf.cell(cell_w, 5, status_text.get(str(item.get("status")), "Unknown"))
            pdf.set_y(y + 49)

    room_for(18)
    pdf.ln(3)
    pdf.set_draw_color(*line)
    pdf.set_line_width(0.2)
    pdf.line(16, pdf.get_y(), pdf.w - 16, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*muted)
    pdf.multi_cell(0, 4, s(
        f"Recorded by {ASSISTANT_NAME} during a live FaceTime walkthrough. Findings come from an AI reading camera "
        "frames and can be wrong or incomplete; review the photos before making any decision about a deposit."))
    return bytes(pdf.output())


def notify_landlord_when_done(pid: str, meeting_id: str, find_check: Any, *, timeout_s: float = 3600.0) -> None:
    """Background: when the call ends, log the report on the property's timeline and text
    the landlord a summary with the report link (when iMessage via Chert is configured and
    the landlord has a number). Never raises."""

    def run() -> None:
        from koov.estates import messaging, store

        deadline = time.monotonic() + timeout_s
        check: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            time.sleep(3.0)
            try:
                check = find_check(pid, meeting_id)
            except Exception:  # noqa: BLE001
                check = None
            if check and check.get("result") != "in_progress":
                break
        else:
            return
        prop = store.get_property(pid) or {}
        url = messaging.public_url(f"/checks/{meeting_id}")
        text = summary_text(prop, check, url)
        sent = False
        if check.get("result") != "no_answer" and (prop.get("landlord") or {}).get("phone"):
            try:
                messaging.send_direct(pid, "landlord", text)
                sent = True
            except Exception as exc:  # noqa: BLE001 - not configured, send failed: the report still exists
                log.info("walkthrough report for %s not texted to the landlord: %s", meeting_id, exc)
        if not sent:
            try:
                store.append_event(pid, {"kind": "system", "direction": "internal", "actor": ASSISTANT_NAME,
                                         "text": text, "meeting_id": meeting_id})
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=run, daemon=True, name=f"check-report-{meeting_id[:8]}").start()
