"""The landlord's report for a walkthrough call: photos, findings, room-by-room result."""
import io
import json

import pytest
from PIL import Image

from koov.estates import checks, store
from koov.inspection import Inspector, LocalInspectionStore

CRACK = {"surface": "counter", "issue": "burn mark on the counter", "severity": "high",
         "box": [100, 100, 600, 600], "say": "There's a burn mark on that counter."}


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 200, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _reply(findings=()):
    return json.dumps({"scene": "kitchen", "view": "clear", "findings": list(findings), "target_seen": True})


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("KOOV_ESTATES_DIR", str(tmp_path / "estates"))
    monkeypatch.setenv("KOOV_NOTES_DIR", str(tmp_path / "notes"))
    (tmp_path / "notes").mkdir()
    from koov import api_server

    monkeypatch.setattr(api_server, "_API_KEY", "secret")  # the report must still be reachable
    return api_server.app.test_client(), tmp_path / "notes"


def _run_tour(notes, mid):
    replies = [_reply([CRACK]), _reply(), _reply(), _reply()]
    insp = Inspector(
        mode="move_out", unit="u", meeting_id=mid, get_frame=_jpeg, speak=lambda s: None,
        store=LocalInspectionStore(str(notes / "inspections")), analyzer=lambda p, i: replies.pop(0),
        interval_s=0, tour="kitchen: counters, floor", evidence_dir=str(notes / "checks" / mid),
    )
    for _ in range(4):
        insp.step()
    (notes / f"worker-status-{mid}.json").write_text(
        json.dumps({"status": "stopped", "meeting_id": mid, "inspection": insp.status()}), encoding="utf-8")
    return insp


def test_tour_saves_a_photo_per_finding_and_per_passed_stop(env):
    _, notes = env
    insp = _run_tour(notes, "m-1")
    status = insp.status()
    assert status["findings"][0]["photo"] == "001.jpg" and status["findings"][0]["room"] == "kitchen"
    items = status["tour"]["rooms"][0]["items"]
    assert [i["status"] for i in items] == ["issue", "ok"] and all(i["photo"] for i in items)
    marked = Image.open(notes / "checks" / "m-1" / "001.jpg").convert("RGB")
    assert marked.getpixel((9, 9)) != (200, 200, 200)  # the damage box was drawn


def test_report_page_shows_findings_rooms_and_photos(env):
    c, notes = env
    prop = store.create_property({"name": "Apt 9", "tenant": {"name": "Joey", "phone": "+15551230000"}})
    store.append_event(prop["id"], {"kind": "facetime", "direction": "out", "actor": "Mechus", "text": "call",
                                    "meeting_id": "m-1", "inspect_mode": "move_out"})
    _run_tour(notes, "m-1")
    r = c.get("/checks/m-1")  # no API key: public by unguessable id
    page = r.get_data(as_text=True)
    assert r.status_code == 200
    for text in ("Apt 9", "Joey", "Move-out check report", "Needs review", "burn mark on the counter",
                 "Room by room", "Kitchen", "Looks good", "Issue noted", "/checks/m-1/photo/001.jpg"):
        assert text in page, text
    assert c.get("/checks/m-1/photo/001.jpg").mimetype == "image/jpeg"
    assert c.get("/checks/m-1/photo/..%2f..%2fworker-status-m-1.json").status_code == 404
    assert c.get("/checks/nope").status_code == 404
    assert "/checks/m-1/report.pdf" in page
    pdf = c.get("/checks/m-1/report.pdf")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf" and pdf.data[:5] == b"%PDF-"
    assert pdf.headers["Content-Disposition"].startswith('attachment; filename="mechus-move-out-Apt-9-')
    assert pdf.data.count(b"/Subtype /Image") >= 2  # the logo and at least one photo are embedded
    assert c.get("/checks/nope/report.pdf").status_code == 404
    check = c.get(f"/api/estates/properties/{prop['id']}/checks", headers={"X-API-Key": "secret"}).get_json()["checks"][0]
    assert check["report_url"] == "/checks/m-1"
    assert checks.summary_text(prop, check) == (
        "Joey's move-out check at Apt 9 needs your review: 1 issue found. 1 of 2 areas checked out, 1 issue.")


def test_headlines():
    prop = {"name": "Apt 4B", "tenant": {"name": "Joey"}}
    ok = {"mode": "move_out", "result": "passed", "findings": [], "rooms": [{"room": "k", "items": [{"status": "ok"}]}]}
    assert checks.headline(prop, ok) == "Joey passed the move-out check at Apt 4B."
    assert "did not connect" in checks.headline(prop, {**ok, "result": "no_answer"})
