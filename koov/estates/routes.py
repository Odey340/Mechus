"""HTTP API for Koov Properties (registered by koov/api_server.py)."""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, Response, current_app, jsonify, redirect, request, send_file, send_from_directory

from koov.chert.client import ChertError, REACTIONS
from koov.chert.webhooks import EventDeduper, InboundMessage, InboundReaction, parse_event, verify_signature
from koov.estates import ai, costs, media, messaging, outbox, pipeline, sample, store

log = logging.getLogger(__name__)

bp = Blueprint("estates", __name__)
_STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD_BYTES = 2 * 1024 ** 3

# Paths that must work without the dashboard API key.
PUBLIC_PREFIXES = ("/r/", "/api/estates/public/", "/webhooks/chert", "/checks/")
PUBLIC_PATHS = ("/properties", "/tenants")


def _err(message: str, status: int, code: str = "") -> tuple[Response, int]:
    return jsonify({"success": False, "error": message, **({"code": code} if code else {})}), status


def api(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, tuple) or isinstance(result, Response):
                return result
            return jsonify({"success": True, **(result if isinstance(result, dict) else {"data": result})})
        except messaging.NotConfigured as exc:
            return _err(str(exc), 409, "chert_not_configured")
        except messaging.SendFailed as exc:
            return jsonify({"success": False, "error": exc.args[0], "code": exc.code or "send_failed",
                            "outbox_id": exc.outbox_id, "event": exc.event}), 502
        except ChertError as exc:
            detail = exc.body.get("error") if isinstance(exc.body, dict) else None
            msg = (detail.get("message") if isinstance(detail, dict) else None) or str(exc)
            return _err(f"Chert: {msg}", 502, exc.code or "chert_error")
        except KeyError:
            return _err("Not found", 404, "not_found")
        except (ValueError, FileNotFoundError) as exc:
            return _err(str(exc), 400, "bad_request")
        except RuntimeError as exc:
            return _err(str(exc), 502, "upstream")
    return wrapper


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@bp.get("/properties")
def page_properties():
    resp = send_from_directory(_STATIC, "properties.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/tenants")
def page_tenants():
    """Tenants live in the dashboard as its first tab."""
    return redirect("/dashboard#/tenants")


def _walkthrough_checks(pid: str) -> list[dict[str, Any]]:
    """Live walkthrough calls for a property, newest first, with what Mechus found.

    Each call is a ``facetime`` timeline event carrying the call's ``meeting_id``; the
    call worker keeps ``worker-status-<meeting_id>.json`` in the notes dir up to date
    with the inspector's findings and tour progress, during the call and after it.
    """
    from koov.notes_paths import resolve_notes_dir

    notes = Path(resolve_notes_dir())
    checks: list[dict[str, Any]] = []
    for ev in store.list_events(pid, limit=500):
        mode = str(ev.get("inspect_mode") or "")
        mid = str(ev.get("meeting_id") or "")
        if ev.get("kind") != "facetime" or mode not in ("move_in", "move_out") or not re.fullmatch(r"[\w.-]{1,80}", mid):
            continue
        status: dict[str, Any] = {}
        try:
            status = json.loads((notes / f"worker-status-{mid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        insp = status.get("inspection") if isinstance(status.get("inspection"), dict) else {}
        tour = insp.get("tour") if isinstance(insp.get("tour"), dict) else {}
        call = str(status.get("status") or "starting")
        ended = call in ("stopped", "error")
        frames = int(insp.get("frames_analysed") or 0)
        verdict = str(insp.get("verdict") or "")
        if call == "error" and not frames:
            result = "no_answer"
        elif not ended:
            result = "in_progress"
        elif verdict == "in_progress" or not verdict:
            result = "incomplete"  # the call ended before the tour did
        else:
            result = verdict
        checks.append({
            "meeting_id": mid, "mode": mode, "at": ev.get("ts"), "call_status": call, "result": result,
            "error": str(status.get("worker_error") or status.get("error") or "") if call == "error" else "",
            "current": tour.get("current") or "", "rooms": tour.get("rooms") or [],
            "not_shown": tour.get("not_shown") or [],
            "findings": [
                {"surface": f.get("surface"), "issue": f.get("issue"), "severity": f.get("severity"), "scene": f.get("scene"),
                 "photo": f.get("photo") or "", "room": f.get("room") or "", "item": f.get("item") or ""}
                for f in (insp.get("findings") or []) if isinstance(f, dict)
            ],
            # A report exists once the call got far enough for Mechus to look at anything.
            "report_url": f"/checks/{mid}" if frames or (ended and call != "error") else "",
        })
    checks.sort(key=lambda c: c.get("at") or 0, reverse=True)
    return checks


def _find_check(pid: str, mid: str) -> dict[str, Any] | None:
    return next((c for c in _walkthrough_checks(pid) if c["meeting_id"] == mid), None)


@bp.get("/checks/<mid>")
def page_check_report(mid: str):
    """The landlord's report for one walkthrough call (public by unguessable id, like /r/<token>)."""
    from koov.estates import checks as check_reports

    for prop in store.list_properties():
        check = _find_check(prop["id"], mid)
        if check is not None:
            resp = Response(check_reports.render(prop, check), mimetype="text/html")
            resp.headers["Cache-Control"] = "no-store"
            return resp
    return Response("Report not found", status=404, mimetype="text/plain")


@bp.get("/checks/<mid>/report.pdf")
def check_report_pdf(mid: str):
    """The same report as a PDF download."""
    from koov.estates import checks as check_reports

    for prop in store.list_properties():
        check = _find_check(prop["id"], mid)
        if check is not None:
            resp = Response(check_reports.render_pdf(prop, check), mimetype="application/pdf")
            resp.headers["Content-Disposition"] = f'attachment; filename="{check_reports.pdf_filename(prop, check)}"'
            resp.headers["Cache-Control"] = "no-store"
            return resp
    return Response("Report not found", status=404, mimetype="text/plain")


@bp.get("/checks/<mid>/photo/<name>")
def check_report_photo(mid: str, name: str):
    from koov.estates import checks as check_reports

    path = check_reports.photo_path(mid, name)
    if path is None:
        return Response("Not found", status=404, mimetype="text/plain")
    return send_file(path, mimetype="image/jpeg", max_age=3600)


@bp.get("/api/estates/checks")
@api
def list_check_reports():
    """Every walkthrough call that produced a report, newest first (the Reports tab)."""
    from koov.estates import checks as check_reports

    rows = []
    for p in store.list_properties():
        if p.get("sample"):
            continue
        for c in _walkthrough_checks(p["id"]):
            if not c["report_url"]:
                continue
            rows.append({
                "meeting_id": c["meeting_id"], "mode": c["mode"], "at": c["at"], "result": c["result"],
                "report_url": c["report_url"], "headline": check_reports.headline(p, c),
                "counts": check_reports.counts(c),
                "property": {"id": p["id"], "name": p.get("name") or ""},
                "tenant": (p.get("tenant") or {}).get("name") or "",
                "landlord_phone": (p.get("landlord") or {}).get("phone") or "",
            })
    rows.sort(key=lambda r: r.get("at") or 0, reverse=True)
    return {"reports": rows}


@bp.post("/api/estates/checks/<mid>/send")
@api
def send_check_report(mid: str):
    """Text the landlord the report summary and link (iMessage via Chert)."""
    from koov.estates import checks as check_reports

    for p in store.list_properties():
        check = _find_check(p["id"], mid)
        if check is None:
            continue
        if not (p.get("landlord") or {}).get("phone"):
            raise ValueError("Add the landlord's phone number to this tenant first (Details).")
        text = check_reports.summary_text(p, check, messaging.public_url(check["report_url"]))
        return {"event": messaging.send_direct(p["id"], "landlord", text), "text": text}
    raise KeyError(mid)


@bp.get("/api/estates/properties/<pid>/checks")
@api
def property_checks(pid: str):
    if store.get_property(pid) is None:
        raise KeyError(pid)
    return {"checks": _walkthrough_checks(pid)}


@bp.get("/api/estates/tenants")
@api
def list_tenants():
    """One row per property: the tenant, and the latest move-in and move-out check."""
    rows = []
    for p in store.list_properties():
        if p.get("sample"):
            continue
        checks = _walkthrough_checks(p["id"])
        rows.append({
            "id": p["id"], "unit": p.get("name") or "", "address": p.get("address") or "",
            "tenant": p.get("tenant") or {},
            "move_in": next((c for c in checks if c["mode"] == "move_in"), None),
            "move_out": next((c for c in checks if c["mode"] == "move_out"), None),
            "history": len(checks),
        })
    return {"tenants": rows}


@bp.get("/r/<token>")
def page_report(token: str):
    if store.inspection_for_token(token) is None:
        return Response("Report not found", status=404, mimetype="text/plain")
    resp = send_from_directory(_STATIC, "report.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Status + overview
# ---------------------------------------------------------------------------

@bp.get("/api/estates/status")
@api
def status():
    from koov.chert import facetime

    s = messaging.status()
    s["facetime_missing"] = facetime.missing_config()
    s["webhook_path"] = "/webhooks/chert"
    s["public_base_url"] = os.getenv("KOOV_PUBLIC_BASE_URL", "")
    return s


def _inspection_card(i: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "property_id", "kind", "label", "status", "stage", "progress", "compare_to", "totals", "created_at",
            "processed_at", "share_token", "error", "reviewed")
    card = {k: i.get(k) for k in keys}
    card["media_count"] = len(i.get("media") or [])
    card["sample"] = bool(i.get("sample"))
    card["rooms"] = len(i.get("rooms") or [])
    thumb = next((f.get("after", {}).get("image") for f in i.get("findings") or [] if (f.get("after") or {}).get("image")), "")
    card["thumb"] = f"/api/estates/public/{i['share_token']}/asset/evidence/{thumb}" if thumb else (
        f"/api/estates/public/{i['share_token']}/asset/frames/{i['frames'][0]['path']}" if i.get("frames") else "")
    return card


@bp.get("/api/estates/overview")
@api
def overview():
    props = store.list_properties()
    week = time.time() - 7 * 86400
    recent, attention = [], []
    open_issues = pending = 0
    chargeable = [0, 0]
    for p in props:
        for ev in store.list_events(p["id"], limit=300):
            ev = {**ev, "property_id": p["id"], "property": p["name"]}
            recent.append(ev)
            cls = ev.get("classification") or {}
            if cls.get("kind") == "maintenance_request" and not ev.get("resolved"):
                open_issues += 1
            if cls.get("urgent") and not ev.get("resolved") and ev.get("ts", 0) > week:
                attention.append({"type": "urgent", "property_id": p["id"], "property": p["name"], "text": cls.get("summary") or ev.get("text"), "ts": ev["ts"], "event_id": ev["id"]})
        for i in store.list_inspections(p["id"]):
            t = i.get("totals") or {}
            if i["status"] == "ready" and t.get("pending_review"):
                pending += 1
                attention.append({"type": "review", "property_id": p["id"], "property": p["name"], "inspection_id": i["id"],
                                  "text": f"{t['pending_review']} finding{'' if t['pending_review'] == 1 else 's'} to review in the {i['kind'].replace('_', '-')} report", "ts": i.get("processed_at") or 0})
            if i["status"] == "failed":
                attention.append({"type": "failed", "property_id": p["id"], "property": p["name"], "inspection_id": i["id"], "text": i.get("error") or "Processing failed", "ts": i.get("created_at", 0)})
            if i["status"] == "ready" and i["kind"] == "move_out":
                chargeable[0] += int(t.get("chargeable_low", 0))
                chargeable[1] += int(t.get("chargeable_high", 0))
    names = {p["id"]: p["name"] for p in props}
    for e in outbox.list_entries(statuses=("failed",)):
        if e.get("pid") in names:
            attention.append({"type": "send_failed", "property_id": e["pid"], "property": names[e["pid"]], "outbox_id": e["id"],
                              "text": f"Message not delivered: {e.get('error') or 'unknown error'}", "ts": e.get("updated_at", 0)})
    recent.sort(key=lambda e: e.get("ts", 0), reverse=True)
    attention.sort(key=lambda a: a.get("ts", 0), reverse=True)

    # Dashboard extras: month-over-month trends, daily activity, people, latest messages.
    now = time.time()
    day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))  # local midnight today
    month, prev = now - 30 * 86400, now - 60 * 86400

    def pct(cur: float, before: float) -> float | None:
        return None if before <= 0 else round((cur - before) / before * 100, 1)

    prop_new = sum(1 for p in props if p.get("created_at", 0) >= month)
    prop_prev_total = sum(1 for p in props if p.get("created_at", 0) < month)
    charge_cur = charge_prev = 0
    activity = {i: {"events": 0, "findings": 0} for i in range(30)}
    for p in props:
        for i in store.list_inspections(p["id"]):
            t, at = i.get("totals") or {}, i.get("processed_at") or 0
            if i["status"] == "ready" and i["kind"] == "move_out":
                if at >= month:
                    charge_cur += int(t.get("chargeable_high", 0))
                elif at >= prev:
                    charge_prev += int(t.get("chargeable_high", 0))
            if i["status"] == "ready" and at >= day0 - 29 * 86400:
                idx = max(0, min(29, int((day0 + 86400 - at) // 86400)))
                activity[idx]["findings"] += int(t.get("findings", 0))
    messages, people, seen = [], [], set()
    for ev in recent:
        if ev.get("ts", 0) >= day0 - 29 * 86400:
            activity[max(0, min(29, int((day0 + 86400 - ev["ts"]) // 86400)))]["events"] += 1
        if ev.get("kind") in ("message", "direct") and ev.get("direction") == "in":
            messages.append(ev)
    for p in props:
        for role, cls in (("tenant", "t"), ("landlord", "l")):
            person = p.get(role) or {}
            key = (person.get("name") or person.get("phone") or "").strip()
            if key and (key, role) not in seen:
                seen.add((key, role))
                people.append({"name": person.get("name") or person.get("phone"), "role": role, "property_id": p["id"], "property": p["name"]})
        for c in p.get("contractors") or []:
            key = (c.get("name") or c.get("phone") or "").strip()
            if key and (key, "contractor") not in seen:
                seen.add((key, "contractor"))
                people.append({"name": c.get("name") or c.get("phone"), "role": "contractor", "property_id": p["id"], "property": p["name"]})
    today_msgs = sum(1 for m in messages if m.get("ts", 0) >= day0)
    return {"kpis": {"properties": len(props), "open_issues": open_issues, "pending_review": pending,
                     "chargeable_low": chargeable[0], "chargeable_high": chargeable[1],
                     "properties_trend": pct(len(props), prop_prev_total) if prop_new else (0.0 if prop_prev_total else None),
                     "chargeable_month": charge_cur, "chargeable_trend": pct(charge_cur, charge_prev)},
            "activity": [{"day_offset": k, **v} for k, v in sorted(activity.items())],
            "people": people[:24], "messages": messages[:12], "today_messages": today_msgs,
            "recent": recent[:20], "attention": attention[:12]}


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _validate_contacts(data: dict[str, Any]) -> None:
    for role in ("landlord", "tenant"):
        phone = ((data.get(role) or {}) if isinstance(data.get(role), dict) else {}).get("phone", "")
        if phone and not messaging.valid_handle(phone):
            raise ValueError(f"{role.capitalize()} phone must be a full number like +15551234567 (or an Apple ID email)")


@bp.get("/api/estates/properties")
@api
def list_properties():
    out = []
    for p in store.list_properties():
        insps = store.list_inspections(p["id"])
        out.append({**p, "inspections": len(insps), "latest": _inspection_card(insps[0]) if insps else None})
    return {"properties": out}


@bp.post("/api/estates/properties")
@api
def create_property():
    data = _body()
    _validate_contacts(data)
    prop = store.create_property(data)
    store.append_event(prop["id"], {"kind": "system", "direction": "internal", "actor": "Mechus", "text": f"Added {prop['name']}"})
    return {"property": prop}


@bp.get("/api/estates/properties/<pid>")
@api
def get_property(pid: str):
    prop = store.get_property(pid)
    if prop is None:
        raise KeyError(pid)
    return {"property": prop, "inspections": [_inspection_card(i) for i in store.list_inspections(pid)]}


@bp.patch("/api/estates/properties/<pid>")
@api
def patch_property(pid: str):
    data = _body()
    _validate_contacts(data)
    return {"property": store.patch_property(pid, data)}


@bp.post("/api/estates/properties/<pid>/contractors")
@api
def add_contractor(pid: str):
    data = _body()
    phone = str(data.get("phone") or "").strip()
    if not messaging.valid_handle(phone):
        raise ValueError("Contractor phone must be a full number like +15551234567")
    entry = {"name": str(data.get("name") or "Contractor").strip()[:80], "phone": phone, "trade": str(data.get("trade") or "")[:40]}
    return {"property": store.update_property(pid, lambda p: p.setdefault("contractors", []).append(entry))}


@bp.get("/api/estates/properties/<pid>/timeline")
@api
def timeline(pid: str):
    if store.get_property(pid) is None:
        raise KeyError(pid)
    return {"events": store.list_events(pid, limit=int(request.args.get("limit", 300)))}


@bp.get("/api/estates/properties/<pid>/inbox/<name>")
def inbox_asset(pid: str, name: str):
    """Photos/videos texted into the property's iMessage group."""
    import re as _re

    if not store.valid_id(pid, "p") or not _re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name or "") or ".." in name:
        return Response("Not found", status=404)
    root = (store.base_dir() / "inbox" / pid).resolve()
    path = (root / name).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        return Response("Not found", status=404)
    return send_file(path, conditional=True, max_age=3600)


@bp.patch("/api/estates/properties/<pid>/events/<eid>")
@api
def patch_event(pid: str, eid: str):
    data = _body()
    ev = store.update_event(pid, eid, lambda e: e.update(resolved=bool(data.get("resolved"))))
    if ev is None:
        raise KeyError(eid)
    return {"event": ev}


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------

@bp.post("/api/estates/properties/<pid>/inspections")
@api
def create_inspection(pid: str):
    insp = store.create_inspection(pid, _body())
    return {"inspection": insp}


@bp.get("/api/estates/inspections/<iid>")
@api
def get_inspection(iid: str):
    insp = store.get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    insp["running"] = pipeline.is_running(iid)
    return {"inspection": insp}


@bp.post("/api/estates/inspections/<iid>/media")
@api
def upload_media(iid: str):
    insp = store.get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    if insp["status"] == "processing":
        raise ValueError("This inspection is processing; wait for it to finish")
    files = request.files.getlist("files")
    if not files:
        raise ValueError("No files received")
    added = []
    ddir = store.media_dir(iid)
    ddir.mkdir(parents=True, exist_ok=True)
    for f in files:
        name = os.path.basename(f.filename or "upload")
        kind = media.media_kind(name, f.mimetype or "")
        if not kind:
            raise ValueError(f"{name}: unsupported file type (use video, photos, or voice memos)")
        mid = store.new_id("m")
        ext = Path(name).suffix.lower() or {"video": ".mp4", "photo": ".jpg", "audio": ".m4a"}[kind]
        dest = ddir / f"{mid}{ext}"
        f.save(dest)
        rec = {"id": mid, "kind": kind, "filename": name[:120], "path": dest.name, "size": dest.stat().st_size,
               "sha256": media.sha256_file(dest), "uploaded_at": media.iso_now(), "source": "upload"}
        if kind == "photo":
            rec.update(media.photo_metadata(dest))
        else:
            pr = media.probe(dest)
            rec.update(duration_s=pr.duration_s, width=pr.width, height=pr.height, captured_at=pr.created_at, has_audio=pr.has_audio)
        added.append(rec)
    store.update_inspection(iid, lambda i: (i["media"].extend(added), i.update(status="draft") if i["status"] in ("failed",) else None))
    return {"media": added}


@bp.delete("/api/estates/inspections/<iid>/media/<mid>")
@api
def delete_media(iid: str, mid: str):
    def apply(i: dict[str, Any]) -> None:
        if i["status"] == "processing":
            raise ValueError("Can't remove files while processing")
        keep = []
        for m in i["media"]:
            if m["id"] == mid:
                try:
                    (store.media_dir(iid) / m["path"]).unlink()
                except OSError:
                    pass
            else:
                keep.append(m)
        i["media"] = keep
    store.update_inspection(iid, apply)
    return {}


@bp.post("/api/estates/inspections/<iid>/process")
@api
def process(iid: str):
    insp = store.get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    if not insp["media"]:
        raise ValueError("Add at least one video or photo first")
    if not pipeline.start(iid):
        raise ValueError("Already processing")
    return {"started": True, "ai_mode": ai.mode()}


@bp.patch("/api/estates/inspections/<iid>/findings/<fid>")
@api
def review_finding(iid: str, fid: str):
    data = _body()

    def apply(i: dict[str, Any]) -> None:
        f = next((x for x in i["findings"] if x["id"] == fid), None)
        if f is None:
            raise KeyError(fid)
        if "review" in data:
            if data["review"] not in store.REVIEW_STATES:
                raise ValueError("review must be pending, confirmed or dismissed")
            f["review"] = data["review"]
        if "category" in data:
            if data["category"] not in store.CATEGORIES:
                raise ValueError("unknown category")
            f["category"] = data["category"]
            f["cost"] = costs.estimate(f)
        if "cost_low" in data or "cost_high" in data:
            low = max(0, int(data.get("cost_low", f["cost"]["low"])))
            high = max(low, int(data.get("cost_high", f["cost"]["high"])))
            f["cost"] = {**f["cost"], "low": low, "high": high, "basis": "Edited by landlord", "chargeable": low > 0 or high > 0}
        if "note" in data:
            f["note"] = str(data["note"] or "")[:400]
        i["totals"] = costs.totals(i["findings"])
        i["reviewed"] = i["totals"]["pending_review"] == 0

    return {"inspection": store.update_inspection(iid, apply)}


@bp.post("/api/estates/inspections/<iid>/audio")
@api
def audio(iid: str):
    return {"audio": pipeline.regenerate_audio(iid, str(_body().get("language") or "en"))}


# ---------------------------------------------------------------------------
# Public report (share token)
# ---------------------------------------------------------------------------

def _public_report(insp: dict[str, Any]) -> dict[str, Any]:
    prop = store.get_property(insp["property_id"]) or {}
    keep = ("id", "kind", "label", "status", "rooms", "findings", "totals", "summary_text", "audio", "processed_at",
            "created_at", "warnings", "share_token", "responses", "transcript")
    out = {k: insp.get(k) for k in keep}
    out["findings"] = [f for f in insp.get("findings") or [] if f.get("review") != "dismissed"]
    out["media"] = [{k: m.get(k) for k in ("id", "kind", "filename", "sha256", "captured_at", "uploaded_at", "duration_s", "source")} for m in insp.get("media") or []]
    out["property"] = {"name": prop.get("name"), "address": prop.get("address"), "unit": prop.get("unit"), "kind": prop.get("kind"),
                       "landlord": (prop.get("landlord") or {}).get("name"), "tenant": (prop.get("tenant") or {}).get("name")}
    base = store.get_inspection(insp["compare_to"]) if insp.get("compare_to") else None
    out["baseline"] = {"processed_at": base.get("processed_at"), "kind": base.get("kind")} if base else None
    return out


@bp.get("/api/estates/public/<token>")
@api
def public_report(token: str):
    insp = store.inspection_for_token(token)
    if insp is None:
        raise KeyError(token)
    return {"report": _public_report(insp)}


@bp.post("/api/estates/public/<token>/response")
@api
def public_response(token: str):
    insp = store.inspection_for_token(token)
    if insp is None:
        raise KeyError(token)
    data = _body()
    fid, resp = str(data.get("finding_id") or ""), str(data.get("response") or "")
    if resp not in ("acknowledge", "dispute"):
        raise ValueError("response must be acknowledge or dispute")
    if not any(f["id"] == fid for f in insp.get("findings") or []):
        raise KeyError(fid)
    entry = {"response": resp, "note": str(data.get("note") or "")[:500], "at": store.now()}
    store.update_inspection(insp["id"], lambda i: i.setdefault("responses", {}).__setitem__(fid, entry))
    store.append_event(insp["property_id"], {"kind": "system", "direction": "in", "actor": "Tenant", "role": "tenant",
                                             "text": f"Tenant {resp}d a finding" + (f": {entry['note']}" if entry["note"] else ""), "inspection_id": insp["id"]})
    return {"response": entry}


@bp.get("/api/estates/public/<token>/asset/<kind>/<name>")
def public_asset(token: str, kind: str, name: str):
    insp = store.inspection_for_token(token)
    path = None
    if insp is not None:
        path = store.resolve_asset(insp["id"], kind, name)
        if path is None and kind == "frames" and insp.get("compare_to"):
            path = store.resolve_asset(insp["compare_to"], kind, name)
    if path is None:
        return Response("Not found", status=404)
    resp = send_file(path, conditional=True, max_age=3600)
    return resp


@bp.delete("/api/estates/properties/<pid>")
@api
def delete_property(pid: str):
    store.delete_property(pid)
    return {}


@bp.delete("/api/estates/inspections/<iid>")
@api
def delete_inspection(iid: str):
    if pipeline.is_running(iid):
        raise ValueError("Wait for processing to finish before deleting")
    store.delete_inspection(iid)
    return {}


@bp.post("/api/estates/sample")
@api
def load_sample():
    prop = sample.create_sample()
    return {"property": prop}


def _norm_item(f: dict[str, Any]) -> str:
    import re as _re

    text = f"{f.get('room', '')}|{(f.get('item') or '').lower()}"
    return _re.sub(r"\s+", " ", text).strip()


@bp.get("/api/estates/reports")
@api
def reports():
    """Every finished report, plus issues that keep coming back across a property's reports."""
    rows, recurring = [], []
    for p in store.list_properties():
        insps = [i for i in store.list_inspections(p["id"]) if i["status"] == "ready"]
        seen: dict[str, list[dict[str, Any]]] = {}
        for i in insps:
            t = i.get("totals") or {}
            rows.append({**_inspection_card(i), "property": {"id": p["id"], "name": p["name"], "sample": bool(p.get("sample"))},
                         "responses": len(i.get("responses") or {}), "disputes": sum(1 for r in (i.get("responses") or {}).values() if r.get("response") == "dispute")})
            for f in i.get("findings") or []:
                if f.get("review") == "dismissed" or not f.get("item") or f.get("category") in ("resolved",):
                    continue
                seen.setdefault(_norm_item(f), []).append({"inspection_id": i["id"], "kind": i["kind"], "category": f["category"],
                                                            "at": i.get("processed_at"), "description": f.get("description")})
        for key, hits in seen.items():
            if len({h["inspection_id"] for h in hits}) >= 2:
                room, item = key.split("|", 1)
                recurring.append({"property_id": p["id"], "property": p["name"], "room": room, "item": item, "count": len(hits),
                                  "hits": sorted(hits, key=lambda h: h.get("at") or 0)})
    rows.sort(key=lambda r: r.get("processed_at") or 0, reverse=True)
    recurring.sort(key=lambda r: -r["count"])
    return {"reports": rows, "recurring": recurring[:20]}


@bp.get("/api/estates/outbox")
@api
def list_outbox():
    pid = request.args.get("property_id", "")
    statuses = tuple(s for s in (request.args.get("status") or "").split(",") if s)
    names = {p["id"]: p["name"] for p in store.list_properties()}
    entries = outbox.list_entries(pid=pid, statuses=statuses)[-200:]
    safe = []
    for e in reversed(entries):
        args = e.get("args") or {}
        preview = args.get("text") or args.get("caption") or ("Photo" if e["op"] == "group_photo" else "Voice note" if e["op"] == "voice_note" else e["op"])
        safe.append({k: e.get(k) for k in ("id", "pid", "op", "status", "attempts", "max_attempts", "error", "error_code", "next_at", "created_at", "updated_at")}
                    | {"property": names.get(e.get("pid"), ""), "preview": str(preview)[:140]})
    return {"entries": safe}


@bp.post("/api/estates/outbox/<oid>/retry")
@api
def retry_outbox(oid: str):
    e = outbox.retry(oid)
    return {"entry": {k: e.get(k) for k in ("id", "status", "error", "attempts")}}


@bp.post("/api/estates/outbox/<oid>/cancel")
@api
def cancel_outbox(oid: str):
    e = outbox.cancel(oid)
    return {"entry": {k: e.get(k) for k in ("id", "status")}}


# ---------------------------------------------------------------------------
# Chert actions
# ---------------------------------------------------------------------------

@bp.post("/api/estates/properties/<pid>/group")
@api
def create_group(pid: str):
    return {"event": messaging.create_group(pid, str(_body().get("intro") or ""))}


@bp.post("/api/estates/properties/<pid>/messages")
@api
def send_message(pid: str):
    if request.files.get("photo"):
        f = request.files["photo"]
        data = f.read()
        if len(data) > 100 * 1024 ** 2:
            raise ValueError("Photos must be under 100 MB")
        return {"event": messaging.send_photo(pid, data, os.path.basename(f.filename or "photo.jpg"), f.mimetype or "image/jpeg",
                                              caption=str(request.form.get("text") or ""))}
    text = str(_body().get("text") or "").strip()
    if not text:
        raise ValueError("Message is empty")
    return {"event": messaging.send_text(pid, text[:4000])}


@bp.post("/api/estates/properties/<pid>/report/<iid>/send")
@api
def send_report(pid: str, iid: str):
    return {"event": messaging.send_report(pid, iid)}


@bp.post("/api/estates/properties/<pid>/voice-note")
@api
def voice_note(pid: str):
    d = _body()
    return {"event": messaging.send_voice_note(pid, str(d.get("inspection_id") or ""), str(d.get("role") or "tenant"), str(d.get("language") or ""))}


@bp.post("/api/estates/properties/<pid>/direct")
@api
def direct(pid: str):
    d = _body()
    text = str(d.get("text") or "").strip()
    if not text:
        raise ValueError("Message is empty")
    role = str(d.get("role") or "tenant")
    if role not in ("landlord", "tenant"):
        raise ValueError("role must be landlord or tenant")
    return {"event": messaging.send_direct(pid, role, text[:4000], str(d.get("effect") or ""))}


@bp.post("/api/estates/properties/<pid>/events/<eid>/react")
@api
def react(pid: str, eid: str):
    reaction = str(_body().get("reaction") or "")
    if reaction not in REACTIONS:
        raise ValueError(f"reaction must be one of {', '.join(REACTIONS)}")
    return {"event": messaging.react(pid, eid, reaction)}


@bp.post("/api/estates/properties/<pid>/events/<eid>/edit")
@api
def edit(pid: str, eid: str):
    text = str(_body().get("text") or "").strip()
    if not text:
        raise ValueError("New text is empty")
    return {"event": messaging.edit(pid, eid, text)}


@bp.post("/api/estates/properties/<pid>/typing")
@api
def typing(pid: str):
    return {"result": messaging.typing(pid)}


@bp.post("/api/estates/properties/<pid>/facetime")
@api
def facetime(pid: str):
    d = _body()

    def _start_agent_call(payload: dict[str, Any]) -> dict[str, Any]:
        # Reuse the API server's own join route so the call gets a session: /api/status,
        # /api/stop and the one-call-per-line guard all work for calls started here.
        headers = {k: request.headers[k] for k in ("X-API-Key", "Authorization") if k in request.headers}
        resp = current_app.test_client().post("/api/join-meeting", json=payload, headers=headers)
        data = resp.get_json(silent=True) or {}
        if resp.status_code >= 400:
            raise RuntimeError(str(data.get("error") or data.get("message") or f"could not start the call (HTTP {resp.status_code})"))
        return data

    call = messaging.facetime(
        pid, str(d.get("role") or "tenant"), bool(d.get("video", True)),
        inspect_mode=str(d.get("inspect_mode") or ""), start_call=_start_agent_call,
    )
    if call.get("inspect_mode") and call.get("meeting_id"):
        from koov.estates import checks as check_reports

        check_reports.notify_landlord_when_done(pid, call["meeting_id"], _find_check)
    return {"call": call}


@bp.post("/api/estates/test-send")
@api
def test_send():
    d = _body()
    phone = str(d.get("phone") or "").strip()
    if not messaging.valid_handle(phone):
        raise ValueError("Enter a full number like +15551234567")
    res = messaging.client().send(phone, str(d.get("text") or "Mechus is connected ✅"), effect=str(d.get("effect") or ""))
    return {"result": res}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _deduper() -> EventDeduper:
    return EventDeduper(store.base_dir() / "chert-events")


@bp.post("/webhooks/chert")
def chert_webhook():
    raw = request.get_data(cache=False)
    secret = (os.getenv("KOOV_CHERT_WEBHOOK_SECRET") or "").strip()
    insecure = (os.getenv("KOOV_CHERT_WEBHOOK_INSECURE") or "").strip() == "1"
    if not (insecure or verify_signature(secret, raw, dict(request.headers))):
        return _err("invalid signature", 401)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return _err("invalid JSON", 400)
    event = parse_event(payload if isinstance(payload, dict) else {})
    event_id = str((payload or {}).get("event_id") or request.headers.get("X-Webhook-Event-Id") or "")
    if event is None:
        return jsonify({"success": True, "ignored": True})
    if event_id and not _deduper().first_time(event_id):
        return jsonify({"success": True, "duplicate": True})
    messaging.note_webhook(type(event).__name__)

    def work() -> None:
        try:
            if isinstance(event, InboundMessage):
                messaging.handle_message(event)
            elif isinstance(event, InboundReaction):
                messaging.handle_reaction(event)
        except Exception:  # noqa: BLE001
            log.exception("chert webhook handling failed")

    threading.Thread(target=work, daemon=True, name="chert-webhook").start()
    return jsonify({"success": True})


def register(app) -> None:
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.register_blueprint(bp)
    try:
        outbox.start()  # resume sends that were pending when the server stopped
    except Exception:  # noqa: BLE001
        log.exception("couldn't resume the Chert outbox")
