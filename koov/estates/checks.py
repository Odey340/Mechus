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
<div class="brandbar"><img src="/static/img/mechus-logo.png" alt="{e(ASSISTANT_NAME)}"><span>{e(kind)} report</span></div>
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
