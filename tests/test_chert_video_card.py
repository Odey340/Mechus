from mechus.chert_livekit import LiveKitVideoCard


def test_inspection_card_is_portrait_and_rgba():
    image = LiveKitVideoCard._draw(2, "move_in", "apt-4b", "Inspection active")
    assert image.size == (720, 1280)
    assert image.mode == "RGBA"


def test_issue_count_changes_rendered_card():
    zero = LiveKitVideoCard._draw(0, "move_in", "apt-4b", "Inspection active")
    three = LiveKitVideoCard._draw(3, "move_in", "apt-4b", "Inspection active")
    assert zero.tobytes() != three.tobytes()
