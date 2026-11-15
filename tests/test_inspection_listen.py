"""The tenant can talk during a tour, and the landlord can get the report by email."""
import array
import json
import math

import pytest

from mechus.estates import checks
from mechus.inspection import Inspector, LocalInspectionStore
from mechus.inspection_listen import TenantListener, UtteranceCutter, rms

TOUR = "kitchen: counters, floor; bathroom: tub or shower"


def make(tmp_path):
    spoken: list[str] = []
    insp = Inspector(
        mode="move_out", unit="t", meeting_id="m", get_frame=lambda: b"jpeg", speak=spoken.append,
        store=LocalInspectionStore(str(tmp_path)), interval_s=0, tour=TOUR,
        analyzer=lambda p, i: json.dumps({"scene": "kitchen", "view": "clear", "findings": [], "target_seen": True}),
    )
    return insp, spoken


def test_skip_moves_to_the_next_stop_and_is_recorded(tmp_path):
    insp, spoken = make(tmp_path)
    assert insp.hear("we don't have counters here") == "skip"
    assert spoken == ["Okay, skipping the counters. Next, show me the floor."]
    item = insp.status()["tour"]["rooms"][0]["items"][0]
    assert item["status"] == "skipped"
    assert insp.status()["tour"]["not_shown"] == ["kitchen: counters"]
    assert insp.status()["heard"][0]["did"] == "skip"


def test_repeat_says_the_current_instruction_again(tmp_path):
    insp, spoken = make(tmp_path)
    assert insp.hear("Sorry, what did you say?") == "repeat"
    assert spoken == ["Show me the counters in the kitchen."]
    assert insp.tour.index == 0


def test_thats_it_ends_the_tour_with_a_summary(tmp_path):
    insp, spoken = make(tmp_path)
    insp.step()
    insp.step()  # counters pass
    assert insp.hear("okay that's it, I'm done") == "finish"
    assert insp.tour.done
    assert spoken[-1] == ("Okay, we'll stop there. That's the whole tour. I found no issues, and 2 areas I couldn't see. "
                          "Thanks for walking me through. You can hang up now.")
    assert insp.status()["verdict"] == "incomplete"
    assert insp.hear("skip") == ""  # nothing left to act on


def test_ordinary_talk_is_ignored(tmp_path):
    insp, spoken = make(tmp_path)
    for line in ("okay this is the kitchen", "hang on a second", "", "the next door is the bathroom"):
        assert insp.hear(line) == ""
    assert spoken == [] and insp.tour.index == 0


def _tone(ms, amplitude, rate=16000):
    n = int(rate * ms / 1000)
    return array.array("h", (int(amplitude * math.sin(i * 0.3)) for i in range(n))).tobytes()


def test_utterance_cutter_returns_speech_after_a_pause():
    cutter = UtteranceCutter(20)
    frame = lambda amp: _tone(20, amp)  # noqa: E731
    assert rms(frame(4000)) > 550 > rms(frame(50))
    out = [cutter.feed(frame(50)) for _ in range(10)]
    out += [cutter.feed(frame(4000)) for _ in range(30)]  # 600 ms of speech
    out += [cutter.feed(frame(50)) for _ in range(40)]    # 800 ms of quiet
    utterances = [o for o in out if o]
    assert len(utterances) == 1 and len(utterances[0]) >= 30 * 640
    # a click shorter than the minimum is not an utterance
    out = [cutter.feed(frame(4000)) for _ in range(8)] + [cutter.feed(frame(50)) for _ in range(40)]
    assert not any(out)


def test_listener_ignores_audio_while_mechus_is_speaking():
    class Frame:
        def __init__(self, pcm16):
            self.pcm16 = pcm16

    loud, quiet = _tone(20, 4000), _tone(20, 50)
    frames = [Frame(loud)] * 30 + [Frame(quiet)] * 40 + [Frame(loud)] * 30 + [Frame(quiet)] * 40
    speaking = {"on": True}
    heard: list[str] = []
    listener = None

    class Audio:
        def read(self, timeout_s=0.5):
            if len(frames) == 70:
                speaking["on"] = False  # Mechus stopped talking before the second utterance
            if not frames:
                listener.stop()
                return None
            return frames.pop(0)

    listener = TenantListener(audio_in=Audio(), transcribe=lambda pcm, rate: "skip", on_text=heard.append,
                              is_speaking=lambda: speaking["on"], sample_rate_hz=16000, frame_ms=20, after_speech_s=0.0)
    listener._run()
    assert heard == ["skip"]


CHECK = {"meeting_id": "m-1", "mode": "move_out", "at": 1794733880, "result": "passed", "error": "", "findings": [],
         "not_shown": [], "rooms": [{"room": "kitchen", "items": [{"item": "floor", "status": "ok", "issues": 0, "photo": ""}]}]}


def test_report_is_emailed_with_the_pdf_when_the_landlord_contact_is_an_email(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def sendmail(self, from_addr, to, message):
            sent["to"], sent["message"] = to, message

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("MECHUS_EMAIL_ENABLED", "1")
    monkeypatch.setenv("MECHUS_EMAIL_USERNAME", "mechus@example.com")
    monkeypatch.setenv("MECHUS_EMAIL_PASSWORD", "app password")
    monkeypatch.delenv("MECHUS_REPORT_EMAIL", raising=False)
    monkeypatch.delenv("MECHUS_REPORT_PHONE", raising=False)
    prop = {"name": "Apt 9", "tenant": {"name": "Joey"}, "landlord": {"phone": "landlord@example.com"}}
    assert checks.deliver_to_landlord("p1", prop, CHECK) == "email"
    assert sent["to"] == ["landlord@example.com"] and sent["tls"] and sent["login"] == "mechus@example.com"
    assert "Subject: Joey passed the move-out check at Apt 9." in sent["message"]
    assert 'filename="mechus-move-out-Apt-9-' in sent["message"] and "application/pdf" in sent["message"]


def test_report_email_setting_gets_every_report(monkeypatch):
    sent = []
    monkeypatch.setattr(checks, "email_report", lambda to, prop, check, url="": sent.append(to))
    monkeypatch.setenv("MECHUS_REPORT_EMAIL", "owner@example.com")
    monkeypatch.delenv("MECHUS_REPORT_PHONE", raising=False)
    assert checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {}}, CHECK) == "email"
    assert checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {"phone": "l@example.com"}}, CHECK) == "email"
    assert sent == ["owner@example.com", "owner@example.com", "l@example.com"]


def test_report_phone_setting_gets_every_report_by_imessage(monkeypatch):
    from mechus.estates import messaging

    texts, emails = [], []
    monkeypatch.setattr(messaging, "send_to_number", lambda pid, phone, text: texts.append((phone, text)))
    monkeypatch.setattr(messaging, "send_direct", lambda pid, role, text: texts.append((role, text)))
    monkeypatch.setattr(checks, "email_report", lambda to, prop, check, url="": emails.append(to))
    monkeypatch.delenv("MECHUS_REPORT_EMAIL", raising=False)
    monkeypatch.setenv("MECHUS_REPORT_PHONE", "555 123 4567")
    assert checks.report_phone() == "+15551234567"
    assert checks.deliver_to_landlord("p1", {"name": "Apt 9", "tenant": {"name": "Joey"}, "landlord": {}}, CHECK) == "imessage"
    assert texts == [("+15551234567", "Joey passed the move-out check at Apt 9. 1 of 1 areas checked out, 0 issues.")]
    # the landlord's own number is messaged too, but not twice when it is the same number
    texts.clear()
    checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {"phone": "+15559990000"}}, CHECK)
    assert [t[0] for t in texts] == ["+15551234567", "landlord"]
    texts.clear()
    checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {"phone": "(555) 123-4567"}}, CHECK)
    assert [t[0] for t in texts] == ["+15551234567"]
    # email still goes out when iMessage is not set up
    monkeypatch.setenv("MECHUS_REPORT_EMAIL", "owner@example.com")
    monkeypatch.setattr(messaging, "send_to_number", lambda *a: (_ for _ in ()).throw(messaging.NotConfigured("no chert")))
    assert checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {}}, CHECK) == "email"
    assert emails == ["owner@example.com"]


def test_delivery_errors_are_readable(monkeypatch):
    monkeypatch.delenv("MECHUS_EMAIL_ENABLED", raising=False)
    monkeypatch.delenv("MECHUS_REPORT_EMAIL", raising=False)
    monkeypatch.delenv("MECHUS_REPORT_PHONE", raising=False)
    with pytest.raises(ValueError, match="landlord's phone number or email"):
        checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {}}, CHECK)
    with pytest.raises(RuntimeError, match="Email isn't set up"):
        checks.deliver_to_landlord("p1", {"name": "Apt 9", "landlord": {"phone": "l@example.com"}}, CHECK)
