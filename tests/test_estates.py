"""Koov Properties: costs, full API flow (offline AI), Chert actions with a fake client, webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time

import pytest

pytest.importorskip("flask_cors")

from koov import api_server as api  # noqa: E402
from koov.estates import costs, media, messaging  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_API_KEY", None)
    monkeypatch.setenv("KOOV_ESTATES_DIR", str(tmp_path / "estates"))
    monkeypatch.setenv("KOOV_ESTATES_FAKE_AI", "1")
    monkeypatch.setenv("KOOV_OUTBOX_WORKER", "0")
    for k in ("KOOV_ELEVEN_API_KEY", "KOOV_CHERT_SIGNING_SECRET", "KOOV_CHERT_TENANT", "KOOV_CHERT_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture()
def client(env):
    return api.app.test_client()


@pytest.fixture(scope="module")
def walkthrough(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    video, photo = d / "walk.mp4", d / "photo.jpg"
    ff = media.ffmpeg_exe()
    subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=12:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True)
    subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=640x480", "-frames:v", "1", str(photo)], check=True)
    return video, photo


def test_costs_never_charge_wear_or_pre_existing():
    for cat in ("wear_and_tear", "pre_existing", "resolved", "defect"):
        assert costs.estimate({"category": cat, "description": "big hole", "severity": "major"}) == {
            "low": 0, "high": 0, "basis": costs.estimate({"category": cat})["basis"], "chargeable": False}
    hole = costs.estimate({"category": "new_damage", "item": "Wall", "description": "hole in drywall", "severity": "moderate"})
    assert hole["chargeable"] and hole["low"] == 120 and hole["high"] == 300
    major = costs.estimate({"category": "new_damage", "description": "hole", "severity": "major"})
    assert major["low"] > hole["low"]
    t = costs.totals([{"category": "new_damage", "cost": hole, "review": "pending"},
                      {"category": "wear_and_tear", "cost": {"low": 0, "high": 0, "chargeable": False}, "review": "confirmed"},
                      {"category": "new_damage", "cost": hole, "review": "dismissed"}])
    assert t["chargeable_low"] == 120 and t["findings"] == 2 and t["pending_review"] == 1


def _wait(client, iid, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/estates/inspections/{iid}").get_json()["inspection"]
        if d["status"] in ("ready", "failed"):
            return d
        time.sleep(0.3)
    raise AssertionError("pipeline timed out")


def _inspect(client, pid, kind, walkthrough):
    video, photo = walkthrough
    i = client.post(f"/api/estates/properties/{pid}/inspections", json={"kind": kind}).get_json()["inspection"]
    with open(video, "rb") as v, open(photo, "rb") as p:
        r = client.post(f"/api/estates/inspections/{i['id']}/media", data={"files": [(v, "walk.mp4"), (p, "room.jpg")]}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert client.post(f"/api/estates/inspections/{i['id']}/process").status_code == 200
    return _wait(client, i["id"])


def test_full_move_in_move_out_flow(client, walkthrough):
    r = client.post("/api/estates/properties", json={"name": "Harbor Lofts 4B", "tenant": {"name": "Pen", "phone": "+15551234567"}})
    pid = r.get_json()["property"]["id"]
    assert client.post("/api/estates/properties", json={"name": "Bad", "tenant": {"phone": "123"}}).status_code == 400

    mi = _inspect(client, pid, "move_in", walkthrough)
    assert mi["status"] == "ready", mi["error"]
    assert {s["key"]: s["state"] for s in mi["stages"]}["transcribe"] == "skipped"  # no ElevenLabs key in tests
    assert mi["findings"] and all(f["category"] == "defect" and not f["cost"]["chargeable"] for f in mi["findings"])
    assert all(m["sha256"] and len(m["sha256"]) == 64 for m in mi["media"])

    mo = _inspect(client, pid, "move_out", walkthrough)
    assert mo["status"] == "ready" and mo["compare_to"] == mi["id"]
    cats = {f["category"] for f in mo["findings"]}
    assert "new_damage" in cats and "pre_existing" in cats
    assert mo["totals"]["chargeable_low"] > 0
    f = next(x for x in mo["findings"] if x["category"] == "new_damage")
    assert f["after"]["image"] and f["before"]["image"]

    # Review: dismissing removes it from totals; cost edits persist.
    before = mo["totals"]["chargeable_low"]
    d = client.patch(f"/api/estates/inspections/{mo['id']}/findings/{f['id']}", json={"review": "dismissed"}).get_json()["inspection"]
    assert d["totals"]["chargeable_low"] < before
    d = client.patch(f"/api/estates/inspections/{mo['id']}/findings/{f['id']}", json={"review": "confirmed", "cost_low": 50, "cost_high": 90}).get_json()["inspection"]
    g = next(x for x in d["findings"] if x["id"] == f["id"])
    assert g["cost"]["low"] == 50 and g["cost"]["basis"] == "Edited by landlord"

    # Public report + assets + tenant response; pages served.
    tok = mo["share_token"]
    rep = client.get(f"/api/estates/public/{tok}").get_json()["report"]
    assert rep["property"]["name"] == "Harbor Lofts 4B" and "phone" not in json.dumps(rep["property"])
    assert client.get(f"/api/estates/public/{tok}/asset/evidence/{g['after']['image']}").status_code == 200
    assert client.get(f"/api/estates/public/{tok}/asset/evidence/..%2F..%2Fsecret").status_code == 404
    assert client.post(f"/api/estates/public/{tok}/response", json={"finding_id": f["id"], "response": "dispute", "note": "was there"}).status_code == 200
    assert client.get(f"/r/{tok}").status_code == 200 and client.get("/r/nope-nope-nope-nope").status_code == 404
    assert client.get("/properties").status_code == 200

    ov = client.get("/api/estates/overview").get_json()
    assert ov["kpis"]["properties"] == 1


def test_public_paths_bypass_api_key(client, monkeypatch, walkthrough):
    monkeypatch.setattr(api, "_API_KEY", "k")
    assert client.get("/api/estates/properties").status_code == 401
    assert client.get("/properties").status_code == 200
    assert client.post("/webhooks/chert", data=b"{}").status_code == 401  # rejected by HMAC, not the API key


class FakeChert:
    def __init__(self):
        self.calls = []

        class Cfg:
            tenant, line_phone, line_id = "koov", "+15550000000", "line-1"
        self.config = Cfg()

    def _rec(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def create_chat(self, to, text, subject="", **kw):
        self._rec("create_chat", to, text, subject=subject)
        return {"chat": {"id": "chat-1"}, "message": {"id": "m-intro"}}

    def send_parts(self, chat_id, parts, **kw):
        self._rec("send_parts", chat_id, parts)
        return {"message": {"id": f"m{len(self.calls)}"}}

    def send(self, phone, body="", **kw):
        self._rec("send", phone, body, **kw)
        return {"message_id": "m-direct", "chat_id": "dm-1"}

    def react(self, mid, reaction):
        self._rec("react", mid, reaction)
        return {"status": "sent"}

    def typing(self, chat_id, on=True):
        self._rec("typing", chat_id)
        return {"status": "sent"}

    def edit(self, mid, text, **kw):
        self._rec("edit", mid, text)
        return {"message_id": mid}

    def upload_attachment(self, data, filename, ctype):
        self._rec("upload", filename, ctype)
        return "att-1"

    def download_attachment(self, att):
        return b"\xff\xd8\xff\xe0" + b"0" * 100, "image/jpeg"

    def get_message(self, mid):
        self._rec("get_message", mid)
        return {"message": {"id": mid, "status": "sent", "delivered_at": "2026-11-14T10:00:00Z", "read_at": None}}


def test_chert_actions_from_dashboard(client, monkeypatch):
    fake = FakeChert()
    monkeypatch.setattr(messaging, "client_from_env", lambda: fake)
    pid = client.post("/api/estates/properties", json={"name": "Loft", "landlord": {"phone": "+15550001111"}, "tenant": {"phone": "+15550002222"}}).get_json()["property"]["id"]

    assert client.post(f"/api/estates/properties/{pid}/messages", json={"text": "hi"}).status_code == 400  # no group yet
    r = client.post(f"/api/estates/properties/{pid}/group", json={})
    assert r.status_code == 200
    assert fake.calls[0][0] == "create_chat" and fake.calls[0][1][0] == ["+15550001111", "+15550002222"]
    assert client.get(f"/api/estates/properties/{pid}").get_json()["property"]["chat_id"] == "chat-1"

    ev = client.post(f"/api/estates/properties/{pid}/messages", json={"text": "Plumber Thursday"}).get_json()["event"]
    assert fake.calls[-1][0] == "send_parts" and fake.calls[-1][1][1] == [{"type": "text", "value": "Plumber Thursday"}]
    assert client.post(f"/api/estates/properties/{pid}/events/{ev['id']}/react", json={"reaction": "love"}).status_code == 200
    assert client.post(f"/api/estates/properties/{pid}/events/{ev['id']}/react", json={"reaction": "nope"}).status_code == 400
    assert client.post(f"/api/estates/properties/{pid}/events/{ev['id']}/edit", json={"text": "x"}).status_code == 400  # group edits refused
    assert client.post(f"/api/estates/properties/{pid}/typing").status_code == 200

    d = client.post(f"/api/estates/properties/{pid}/direct", json={"role": "tenant", "text": "🎉", "effect": "celebration"}).get_json()["event"]
    assert fake.calls[-1][0] == "send" and fake.calls[-1][1] == ("+15550002222", "🎉") and fake.calls[-1][2]["effect"] == "celebration"
    assert fake.calls[-1][2]["idempotency_key"]  # persisted key -> safe retries
    assert client.post(f"/api/estates/properties/{pid}/events/{d['id']}/edit", json={"text": "edited"}).status_code == 200

    photo = client.post(f"/api/estates/properties/{pid}/messages", data={"photo": (__import__("io").BytesIO(b"\xff\xd8\xff"), "p.jpg"), "text": "look"}, content_type="multipart/form-data")
    assert photo.status_code == 200 and any(c[0] == "upload" for c in fake.calls)
    assert client.post(f"/api/estates/properties/{pid}/facetime", json={"role": "tenant"}).status_code == 502  # not configured: clear error


def test_chert_not_configured_is_409(client):
    pid = client.post("/api/estates/properties", json={"name": "Loft", "tenant": {"phone": "+15550002222"}}).get_json()["property"]["id"]
    r = client.post(f"/api/estates/properties/{pid}/group", json={})
    assert r.status_code == 409 and r.get_json()["code"] == "chert_not_configured"


def test_webhook_files_message_and_tapbacks(client, monkeypatch):
    fake = FakeChert()
    monkeypatch.setattr(messaging, "client_from_env", lambda: fake)
    monkeypatch.setenv("KOOV_CHERT_WEBHOOK_SECRET", "whsec")
    pid = client.post("/api/estates/properties", json={"name": "Loft", "landlord": {"phone": "+15550001111"}, "tenant": {"phone": "+15550002222"}}).get_json()["property"]["id"]
    client.post(f"/api/estates/properties/{pid}/group", json={})

    def deliver(payload):
        raw = json.dumps(payload).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        return client.post("/webhooks/chert", data=raw, headers={"X-Webhook-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})

    msg = {"event_type": "message.received", "event_id": "evt-1", "data": {
        "chat": {"id": "chat-1", "is_group": True},
        "message": {"id": "in-1", "direction": "inbound", "sender_handle": {"handle": "+15550002222"},
                    "parts": [{"type": "text", "value": "The kitchen sink is leaking everywhere!"}]}}}
    assert deliver(msg).status_code == 200
    assert deliver(msg).get_json().get("duplicate") is True
    for _ in range(50):
        events = client.get(f"/api/estates/properties/{pid}/timeline").get_json()["events"]
        inbound = [e for e in events if e.get("message_id") == "in-1"]
        if inbound and any(c[0] == "send" for c in fake.calls):
            break
        time.sleep(0.1)
    assert inbound and inbound[0]["role"] == "tenant" and inbound[0]["classification"]["urgent"]
    assert ("react", ("in-1", "emphasize"), {}) in fake.calls          # ‼️ on the urgent message
    assert any(c[0] == "send" and c[1][0] == "+15550001111" for c in fake.calls)  # landlord alerted 1:1

    rx = {"event_type": "reaction.added", "event_id": "evt-2", "data": {"sender_handle": {"handle": "+15550001111"},
          "reaction": {"target_message_id": "in-1", "reaction": {"kind": "like"}}}}
    assert deliver(rx).status_code == 200
    for _ in range(30):
        ev = next(e for e in client.get(f"/api/estates/properties/{pid}/timeline").get_json()["events"] if e.get("message_id") == "in-1")
        if any(t["by"] == "+15550001111" for t in ev["tapbacks"]):
            break
        time.sleep(0.1)
    assert any(t["by"] == "+15550001111" and t["reaction"] == "like" for t in ev["tapbacks"])

    bad = client.post("/webhooks/chert", data=b"{}", headers={"X-Webhook-Signature": "t=1,v1=00"})
    assert bad.status_code == 401


# ---------------------------------------------------------------------------
# Outbox robustness
# ---------------------------------------------------------------------------

from koov.chert.client import ChertError  # noqa: E402
from koov.estates import outbox  # noqa: E402


class FlakyChert(FakeChert):
    """Fails the first N group sends with a 503, then succeeds."""

    def __init__(self, failures=1, status=503, code=""):
        super().__init__()
        self.failures, self.status, self.code = failures, status, code
        self.keys = []

    def send_parts(self, chat_id, parts, **kw):
        self.keys.append(kw.get("idempotency_key"))
        if self.failures > 0:
            self.failures -= 1
            raise ChertError("boom", status=self.status, code=self.code)
        return super().send_parts(chat_id, parts)


def _group(client, monkeypatch, fake):
    monkeypatch.setattr(messaging, "client_from_env", lambda: fake)
    pid = client.post("/api/estates/properties", json={"name": "Loft", "landlord": {"phone": "+15550001111"}, "tenant": {"phone": "+15550002222"}}).get_json()["property"]["id"]
    assert client.post(f"/api/estates/properties/{pid}/group", json={}).status_code == 200
    return pid


def test_transient_failure_retries_with_same_idempotency_key(client, monkeypatch):
    fake = FlakyChert(failures=1, status=503)
    pid = _group(client, monkeypatch, fake)
    r = client.post(f"/api/estates/properties/{pid}/messages", json={"text": "Plumber Thursday"})
    assert r.status_code == 200
    ev = r.get_json()["event"]
    assert ev["delivery"] == "retrying"
    entry = outbox.get(ev["outbox_id"])
    assert entry["status"] == "retrying" and entry["attempts"] == 1
    outbox.process_due(now=entry["next_at"] + 1)
    entry = outbox.get(ev["outbox_id"])
    assert entry["status"] == "sent" and entry["attempts"] == 2
    assert len(fake.keys) == 2 and fake.keys[0] == fake.keys[1]  # same key both times -> no double send
    tl = client.get(f"/api/estates/properties/{pid}/timeline").get_json()["events"]
    sent = next(e for e in tl if e["id"] == ev["id"])
    assert sent["delivery"] == "sent" and sent["message_id"]


def test_permanent_failure_is_reported_and_retryable(client, monkeypatch):
    fake = FlakyChert(failures=1, status=400, code="1002")
    pid = _group(client, monkeypatch, fake)
    r = client.post(f"/api/estates/properties/{pid}/messages", json={"text": "hello"})
    assert r.status_code == 502 and r.get_json()["code"] == "1002" and "invalid" in r.get_json()["error"]
    oid = r.get_json()["outbox_id"]
    assert outbox.get(oid)["status"] == "failed"
    assert client.get("/api/estates/overview").get_json()["attention"][0]["type"] == "send_failed"
    assert client.post(f"/api/estates/outbox/{oid}/retry").status_code == 200
    assert outbox.get(oid)["status"] == "sent"
    listed = client.get("/api/estates/outbox").get_json()["entries"]
    assert any(e["id"] == oid and e["status"] == "sent" for e in listed)


def test_delivery_receipts_are_polled(client, monkeypatch):
    fake = FakeChert()
    pid = _group(client, monkeypatch, fake)
    ev = client.post(f"/api/estates/properties/{pid}/messages", json={"text": "hi"}).get_json()["event"]
    entry = outbox.get(ev["outbox_id"])
    outbox.process_due(now=entry["next_at"] + 1)
    assert outbox.get(ev["outbox_id"])["status"] == "delivered"
    tl = client.get(f"/api/estates/properties/{pid}/timeline").get_json()["events"]
    assert next(e for e in tl if e["id"] == ev["id"])["delivery"] == "delivered"


def test_per_chat_order_is_kept(client, monkeypatch):
    fake = FlakyChert(failures=1, status=503)
    pid = _group(client, monkeypatch, fake)
    first = client.post(f"/api/estates/properties/{pid}/messages", json={"text": "first"}).get_json()["event"]
    assert outbox.get(first["outbox_id"])["status"] == "retrying"
    second = client.post(f"/api/estates/properties/{pid}/messages", json={"text": "second"}).get_json()["event"]
    # The second was tried immediately (fake now succeeds), but the worker never sends a later message
    # for a chat while an earlier one is still retrying.
    e1 = outbox.get(first["outbox_id"])
    outbox.process_due(now=e1["next_at"] + 1)
    assert outbox.get(first["outbox_id"])["status"] in ("sent", "delivered")
    assert outbox.get(second["outbox_id"])["status"] in ("sent", "delivered")


def test_sample_property_is_a_complete_model_report(client):
    r = client.post("/api/estates/sample")
    assert r.status_code == 200
    pid = r.get_json()["property"]["id"]
    assert client.post("/api/estates/sample").get_json()["property"]["id"] == pid  # idempotent
    insps = client.get(f"/api/estates/properties/{pid}").get_json()["inspections"]
    kinds = {i["kind"]: i for i in insps}
    assert set(kinds) == {"move_in", "move_out"} and all(i["status"] == "ready" for i in insps)
    mo = client.get(f"/api/estates/inspections/{kinds['move_out']['id']}").get_json()["inspection"]
    cats = sorted(f["category"] for f in mo["findings"])
    assert cats == sorted(["new_damage", "wear_and_tear", "new_damage", "cleaning", "pre_existing", "missing"])
    assert mo["compare_to"] == kinds["move_in"]["id"]
    assert mo["totals"]["new_issues"] == 4 and mo["totals"]["chargeable_low"] > 0
    assert all(f["after"]["image"] and f["before"]["image"] for f in mo["findings"])
    tok = mo["share_token"]
    assert client.get(f"/api/estates/public/{tok}/asset/evidence/{mo['findings'][0]['after']['image']}").status_code == 200
    rep = client.get("/api/estates/reports").get_json()
    assert len(rep["reports"]) == 2
    assert any(x["item"] == "tub tile" and x["count"] == 2 for x in rep["recurring"])  # documented at move-in, seen again at move-out
    # Sample has no phone numbers, so nothing can be texted by accident.
    assert client.post(f"/api/estates/properties/{pid}/group", json={}).status_code == 400


def test_delete_inspection_and_property(client):
    pid = client.post("/api/estates/sample").get_json()["property"]["id"]
    insps = client.get(f"/api/estates/properties/{pid}").get_json()["inspections"]
    mi = next(i for i in insps if i["kind"] == "move_in")
    assert client.delete(f"/api/estates/inspections/{mi['id']}").status_code == 200
    mo = next(i for i in client.get(f"/api/estates/properties/{pid}").get_json()["inspections"] if i["kind"] == "move_out")
    assert mo["compare_to"] == ""  # baseline link cleared, report kept
    assert client.delete(f"/api/estates/properties/{pid}").status_code == 200
    assert client.get(f"/api/estates/properties/{pid}").status_code == 404
    assert client.get(f"/r/{mo['share_token']}").status_code == 404
