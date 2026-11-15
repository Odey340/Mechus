"""Deterministic real-time data helpers."""

from __future__ import annotations

import json
from unittest.mock import patch

from mechus.realtime import (
    _expand_weather_query_locations,
    _extract_weather_location,
    _strip_weather_location_noise,
    _time_search,
    _weather_search,
    _with_search_freshness,
    build_search_query,
    is_vague_search_request,
    needs_realtime,
    needs_web_lookup,
)


def test_extract_weather_location_common_phrasings() -> None:
    assert _extract_weather_location("what is the weather like in Solon, Ohio?") == "solon, ohio"
    assert _extract_weather_location("weather in Fremont California") == "fremont california"
    assert _extract_weather_location("how cold is it in Chicago?") == "chicago"
    assert _strip_weather_location_noise("india right now") == "india"


def test_weather_country_capital_variant() -> None:
    variants = _expand_weather_query_locations("india")
    assert "New Delhi,IN" in variants
    assert "india" in variants


def test_needs_web_lookup_jarvis_phrases() -> None:
    assert needs_web_lookup("find me a good 4k monitor")
    assert needs_web_lookup("can you find some for me")
    assert needs_web_lookup("what's the most popular game right now")
    assert needs_realtime("pull that up for me")
    assert not needs_web_lookup("what did we decide about the budget")


def test_substantive_find_me_not_vague() -> None:
    q = "can you find me mexican restaurants in montgomery county"
    assert not is_vague_search_request(q)
    enriched = build_search_query(
        q,
        last_user="where can i get a taco for 5 in poolesville maryland",
        last_mechus="try the taco bar",
    )
    assert enriched == q


def test_research_triggers_web_lookup() -> None:
    assert needs_web_lookup("can you research the name of that nonprofit")


def test_who_won_not_vague_and_skips_dialog_bleed() -> None:
    assert not is_vague_search_request("who won the")
    enriched = build_search_query(
        "who won the",
        last_user="How didn't you see who I am?",
        last_mechus="Gautam was surprised",
    )
    assert "gautam" not in enriched.lower()
    assert "how didn" not in enriched.lower()
    assert "who won" in enriched.lower()
    assert _with_search_freshness("nba finals score") != "nba finals score"


def test_build_search_query_uses_dialog_context() -> None:
    q = build_search_query(
        "can you find some for me",
        last_user="what is the best 4k 240hz monitor",
        last_mechus="I'd look at the ASUS PG27AQDM.",
    )
    assert "4k" in q.lower() or "240hz" in q.lower() or "monitor" in q.lower()
    assert build_search_query(
        "best gaming monitor under 500",
        last_user="earlier topic",
    ) == "best gaming monitor under 500"


def test_weather_search_marks_current_only() -> None:
    payload = {
        "cod": 200,
        "name": "Solon",
        "main": {"temp": 53.2, "feels_like": 49.8},
        "weather": [{"description": "clear sky"}],
        "wind": {"speed": 10.7, "deg": 5},
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=_Resp()):
        out = _weather_search("what is the weather like in Solon, Ohio?", "key")

    assert out.startswith("CURRENT_WEATHER: It's 53 degrees")
    assert "clear sky in Solon" in out
    assert "OpenWeatherMap" not in out
    assert "no forecast" not in out.lower()


def test_bare_weather_does_not_extract_time_as_place() -> None:
    from mechus.realtime import _extract_weather_location, _is_bare_weather_query

    assert _extract_weather_location("what is the time right now") == ""
    assert _extract_weather_location("whats the weather like right now") == ""
    assert _is_bare_weather_query("whats the weather like right now") is True


def test_weather_city_from_context_rejects_noise() -> None:
    from mechus.realtime import _weather_city_from_context

    assert _weather_city_from_context(context_location="time", fallback_timezone="America/New_York") == "New York,US"


def test_bare_time_query_uses_context_location() -> None:
    out = _time_search(
        "what's the time right now",
        context_location="solon, ohio",
    )
    assert out.startswith("CURRENT_TIME:")
    assert "Solon" in out
    assert "it's" in out.lower()
    assert "device's clock" not in out


def test_bare_time_query_uses_fallback_timezone() -> None:
    out = _time_search(
        "what's the time right now",
        fallback_timezone="America/New_York",
    )
    assert out.startswith("CURRENT_TIME:")
    assert "it's" in out.lower()
    assert "device's clock" not in out


def test_multi_location_time_query() -> None:
    out = _time_search("what's the time in seoul and ohio")
    assert out.startswith("CURRENT_TIME:")
    low = out.lower()
    assert "seoul" in low
    assert "ohio" in low
    assert " and " in low
    assert "seoul and ohio is" not in low


def test_tell_me_about_solar_system_is_general_not_recap() -> None:
    from mechus.intent import INTENT_GENERAL, INTENT_RECAP, classify_intent

    assert classify_intent("tell me about the solar system") == INTENT_GENERAL
    assert classify_intent("tell me about what we discussed") == INTENT_RECAP


def test_bare_weather_query_uses_fallback_timezone() -> None:
    from mechus.realtime import _is_bare_weather_query, _weather_city_from_context

    assert _is_bare_weather_query("what's the weather right now")
    assert _weather_city_from_context(fallback_timezone="America/New_York") == "New York,US"
