"""Guided tour: Mechus leads room by room, one stop at a time, and wraps up."""
import json

from mechus.inspection import Inspector, LocalInspectionStore, Tour, build_prompt, parse_target_seen, parse_tour

TOUR = "kitchen: counters, floor; bathroom: sink"
CRACK = {"surface": "counter", "issue": "burn mark on the counter", "severity": "medium",
         "box": [1, 1, 9, 9], "say": "There's a burn mark on that counter."}


def reply(seen=True, view="clear", findings=(), ask=""):
    return json.dumps({"scene": "kitchen", "view": view, "findings": list(findings), "ask": ask, "target_seen": seen})


def make(tmp_path, replies, *, mode="move_in", timeout=999.0):
    spoken: list[str] = []
    queue = list(replies)
    insp = Inspector(
        mode=mode, unit="t", meeting_id="m", get_frame=lambda: b"jpeg", speak=spoken.append,
        store=LocalInspectionStore(str(tmp_path)), analyzer=lambda p, i: queue.pop(0), interval_s=0, tour=TOUR,
    )
    insp._ask_gap_s = 0.0
    insp.tour.item_timeout_s = timeout
    return insp, spoken


def test_parse_tour():
    assert parse_tour(TOUR) == [("kitchen", ["counters", "floor"]), ("bathroom", ["sink"])]
    assert parse_tour("off") == []
    assert parse_tour("Hall") == [("hall", ["walls", "floor"])]
    assert len(parse_tour("")) == 4  # the default tour
    assert parse_target_seen('{"target_seen": true}') and not parse_target_seen('{"scene": "x"}')


def test_tour_walks_every_stop_in_order_and_wraps_up(tmp_path):
    insp, spoken = make(tmp_path, [reply()] * 6)
    assert insp.tour.opening() == ("Let's start in the kitchen. Show me the counters, nice and slow. "
                                   "If something doesn't apply, just say skip.")
    for _ in range(6):
        insp.step()
    assert spoken == [
        "Okay, the counters look good. Next, show me the floor.",
        "Okay, the floor looks good. That's the kitchen done. Now take me to the bathroom, and show me the sink.",
        "Okay, the sink looks good. That's the whole tour. I found no issues. "
        "Thanks for walking me through. You can hang up now.",
    ]
    assert insp.tour.done and insp.status()["verdict"] == "passed"
    assert insp.card_status() == "Tour complete"


def test_one_glance_is_not_enough_and_wrong_area_does_not_count(tmp_path):
    insp, spoken = make(tmp_path, [reply(), reply(seen=False), reply(view="partial")])
    for _ in range(3):
        insp.step()
    assert spoken == [] and insp.tour.index == 0


def test_damage_is_called_out_then_the_tour_moves_on(tmp_path):
    insp, spoken = make(tmp_path, [reply(findings=[CRACK]), reply(seen=False)])
    insp.step()
    insp.step()
    assert spoken == [CRACK["say"], "Got it, that's noted for the counters. Next, show me the floor."]
    assert insp.status()["verdict"] == "needs_review"
    assert insp.status()["tour"]["rooms"][0]["items"][0] == {"item": "counters", "status": "issue", "issues": 1, "photo": ""}


def test_a_stop_that_never_appears_gets_one_reminder_then_is_skipped(tmp_path):
    insp, spoken = make(tmp_path, [reply(seen=False)] * 2, timeout=0.0)
    insp.step()
    insp.step()
    assert spoken == [
        "I still need to see the counters in the kitchen. Point the camera at it for me.",
        "I'll mark the counters as not shown. Next, show me the floor.",
    ]
    assert insp.status()["tour"]["not_shown"] == ["kitchen: counters"]


def test_asks_once_for_a_better_look_at_the_current_stop(tmp_path):
    insp, spoken = make(tmp_path, [reply(seen=False, view="partial", ask="Move in closer on that for me.")] * 3)
    for _ in range(3):
        insp.step()
    assert spoken == ["Move in closer on that for me."]


def test_move_out_wording_and_incomplete_verdict():
    tour = Tour(parse_tour("kitchen: floor, counters"), mode="move_out", item_timeout_s=0.0)
    tour.seen()
    assert tour.seen() == "Okay, the floor matches move-in. Next, show me the counters."
    tour.tick()
    assert "not shown" in tour.tick() and tour.done
    assert tour.verdict() == "incomplete"


def test_prompt_names_the_current_stop():
    prompt = build_prompt("move_in", [], [], target={"room": "kitchen", "item": "floor"})
    assert "the floor in the kitchen" in prompt and "target_seen" in prompt
