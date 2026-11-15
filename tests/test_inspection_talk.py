"""Inspection talking style: asks for a better look, confirms clean areas, never babbles."""
import json

from koov.inspection import Inspector, LocalInspectionStore, build_prompt, parse_talk


def reply(scene="living room north wall", view="clear", findings=(), ask="", ok=""):
    return json.dumps({"scene": scene, "view": view, "findings": list(findings), "ask": ask, "ok": ok})


CRACK = {"surface": "wall", "issue": "crack above the window", "severity": "medium",
         "box": [100, 100, 300, 300], "say": "There's a crack above that window."}


def make(tmp_path, replies, *, ask_gap=0.0, chat_gap=0.0):
    spoken: list[str] = []
    queue = list(replies)
    insp = Inspector(
        mode="move_in", unit="t", meeting_id="m", get_frame=lambda: b"jpeg",
        speak=spoken.append, store=LocalInspectionStore(str(tmp_path)),
        analyzer=lambda prompt, images: queue.pop(0), interval_s=0, tour="off",
    )
    insp._ask_gap_s, insp._chat_gap_s = ask_gap, chat_gap
    return insp, spoken


def test_parse_talk_defaults_for_old_shape_replies():
    assert parse_talk('{"scene": "hall", "findings": []}') == ("clear", "", "")
    assert parse_talk("not json") == ("clear", "", "")
    assert parse_talk(reply(view="PARTIAL", ask=" Show me that again? ")) == ("partial", "Show me that again?", "")


def test_all_clear_needs_two_clean_looks_and_is_said_once_per_area(tmp_path):
    ok = "Okay, everything looks good there."
    insp, spoken = make(tmp_path, [reply(ok=ok)] * 4)
    insp.step()
    assert spoken == []  # a single passing glance is not signed off
    insp.step()
    assert spoken == [ok]
    insp.step()
    insp.step()
    assert spoken == [ok]  # same wall: not confirmed again
    assert insp.status()["areas_cleared"] == ["living room north wall"]


def test_asks_for_a_second_look_when_the_view_is_poor(tmp_path):
    insp, spoken = make(tmp_path, [reply(view="partial", ask="Can you show me that wall again?", ok="Looks good.")])
    insp.step()
    assert spoken == ["Can you show me that wall again?"]  # never an all-clear on a poor view
    assert insp.said[0]["kind"] == "ask"


def test_does_not_repeat_a_question_or_nag_about_one_spot(tmp_path):
    asks = ["Can you show me that wall again?", "Show me that wall again please?",
            "Move closer to the corner by the window.", "Tilt up toward the ceiling there."]
    insp, spoken = make(tmp_path, [reply(view="partial", ask=a) for a in asks])
    for _ in asks:
        insp.step()
    assert spoken == [asks[0], asks[2]]  # the rephrased repeat is dropped; then two per scene is the cap


def test_findings_come_first_and_are_not_followed_by_an_all_clear(tmp_path):
    insp, spoken = make(tmp_path, [reply(findings=[CRACK], ask="What's behind that door?", ok="Looks good.")])
    insp.step()
    assert spoken == [CRACK["say"]]
    assert len(insp.announced) == 1 and insp.said == []


def test_stays_quiet_until_the_gap_has_passed(tmp_path):
    insp, spoken = make(
        tmp_path, [reply(view="unclear", ask="Flip the camera around for me.")] + [reply(ok="Fine.")] * 3,
        ask_gap=60, chat_gap=60,
    )
    for _ in range(4):
        insp.step()
    assert spoken == []


def test_prompt_carries_what_was_said_and_cleared():
    prompt = build_prompt("move_in", [], [], said=["Can you show me that wall again?"], cleared=["kitchen counter"])
    assert "Can you show me that wall again?" in prompt
    assert "kitchen counter" in prompt
    assert "Mechus" in prompt
