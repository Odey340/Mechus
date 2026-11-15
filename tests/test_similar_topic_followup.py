"""Similar-topic follow-up detection for answer variation."""

from koov.dialog_memory import is_similar_topic_followup


def test_favorite_planet_and_pick_are_similar():
    assert is_similar_topic_followup(
        "what would you pick if you had to pick",
        "what's your favorite planet",
    )


def test_favorite_color_differs_from_planet():
    assert not is_similar_topic_followup(
        "what's your favorite color",
        "what's your favorite planet",
    )


def test_action_items_same_topic():
    assert is_similar_topic_followup(
        "what are the action items for sahasra",
        "what are the action items",
    )
