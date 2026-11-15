"""
Real-time data fetcher for Koov.
Handles web search, weather, and news queries.
"""
from __future__ import annotations
from datetime import datetime
import json
import logging
import os
import re
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

try:
    import tzdata  # noqa: F401  # IANA tz DB for zoneinfo on minimal Docker images
except ImportError:
    pass

log = logging.getLogger(__name__)


def _now_in_tz(tz_name: str) -> datetime | None:
    """Local wall-clock time in *tz_name*, or None when IANA data is unavailable."""
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception as exc:
        log.debug("ZoneInfo(%s) unavailable: %s", tz_name, exc)
        return None


def _tavily_search(query: str, api_key: str, max_results: int = 3) -> str:
    """Search the web using Tavily and return a summary."""
    url = "https://api.tavily.com/search"
    body = json.dumps({
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Use Tavily's direct answer if available
        answer = (data.get("answer") or "").strip()
        if answer:
            return answer
        # Fall back to top result snippets
        results = data.get("results") or []
        parts = []
        for r in results[:2]:
            content = (r.get("content") or "")[:200]
            source = r.get("url", "")
            try:
                domain = source.split("/")[2].replace("www.", "")
            except Exception:
                domain = ""
            if content:
                parts.append(f"{content} (Source: {domain})" if domain else content)
        return " ".join(parts).strip() or "No results found."
    except Exception as exc:
        log.warning("Tavily search failed: %s", exc)
        return ""


_WEATHER_LOCATION_NOISE = frozenset(
    {
        "time",
        "whats",
        "what",
        "now",
        "like",
        "weather",
        "temperature",
        "currently",
        "right",
        "the",
    }
)


def _weather_has_explicit_place(query: str) -> bool:
    """True when the question names a city/region (``weather in Boston``)."""
    low = " ".join((query or "").strip().split()).lower()
    if not low:
        return False
    return bool(
        re.search(r"\bweather\s+(?:like\s+)?(?:in|for|at)\s+\S", low)
        or re.search(r"\btemperature\s+(?:in|for|at)\s+\S", low)
        or re.search(
            r"\b(?:how\s+hot|how\s+cold)\s+(?:is\s+it\s+)?(?:in|for|at)\s+\S",
            low,
        )
    )


def _extract_weather_location(query: str) -> str:
    """Extract the requested place from common weather question phrasings."""
    if not _weather_has_explicit_place(query):
        return ""
    q = " ".join((query or "").strip().split())
    if not q:
        return ""
    low = q.lower()
    patterns = (
        r"\bweather\s+(?:like\s+)?(?:in|for|at)\s+(.+)$",
        r"\btemperature\s+(?:in|for|at)\s+(.+)$",
        r"\b(?:how\s+hot|how\s+cold)\s+(?:is\s+it\s+)?(?:in|for|at)\s+(.+)$",
    )
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            loc = re.sub(r"\s+", " ", m.group(1)).strip(" ?.!,")
            loc = _strip_weather_location_noise(loc)
            if loc.lower() in _WEATHER_LOCATION_NOISE:
                return ""
            return loc
    cleaned = re.sub(
        r"\b(?:what'?s|whats|what\s+is|tell\s+me|current|currently|right\s+now|the|like|weather|temperature)\b",
        " ",
        low,
    )
    loc = re.sub(r"\s+", " ", cleaned).strip(" ?.!,")
    loc = _strip_weather_location_noise(loc)
    if loc.lower() in _WEATHER_LOCATION_NOISE:
        return ""
    return loc


def _strip_weather_location_noise(location: str) -> str:
    """Drop trailing temporal filler so 'india right now' resolves as a place."""
    s = " ".join((location or "").split()).strip(" ?.!,")
    if not s:
        return ""
    low = s.lower()
    for phrase in ("right now", "currently", "today", "at the moment", "now"):
        if low.endswith(phrase):
            s = s[: -len(phrase)].strip(" ?.!,")
            low = s.lower()
    return s


def _wind_direction(degrees: float | int | None) -> str:
    if degrees is None:
        return ""
    try:
        deg = float(degrees) % 360.0
    except (TypeError, ValueError):
        return ""
    dirs = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
    return dirs[int((deg + 22.5) // 45) % 8]


def _expand_weather_query_locations(location: str) -> list[str]:
    """Generate OpenWeatherMap-friendly query variants.

    OpenWeatherMap's `q=` parameter accepts ``city`` or ``city,country`` or
    ``city,state,country`` (US only) — but it can fail on a free-text city
    like "Solon, Ohio" because the comma confuses the matcher. We expand the
    raw user phrase into a few likely keys so small US cities resolve.
    """
    if not location:
        return []
    raw = _strip_weather_location_noise(" ".join(location.split()))
    raw_lower = raw.lower()
    out: list[str] = []
    if raw_lower in _COUNTRY_WEATHER_CAPITAL:
        out.append(_COUNTRY_WEATHER_CAPITAL[raw_lower])
    out.append(raw)
    chunks = [c.strip() for c in raw.split(",") if c.strip()]
    if len(chunks) == 1:
        # No state/country given. Leave as-is; OWM will try a global match.
        return out
    if len(chunks) >= 2:
        first, second = chunks[0], chunks[1].lower()
        # If the second chunk is a US state name/abbrev → assume US.
        if second in _US_STATE_TO_TZ:
            # OWM accepts "city,state-code,US" with the postal abbreviation.
            state_abbrev = second
            if len(state_abbrev) > 2:
                # Map full state name to a 2-letter code where possible.
                _US_STATE_NAME_TO_CODE = {
                    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
                    "california": "ca", "colorado": "co", "connecticut": "ct",
                    "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi",
                    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
                    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me",
                    "maryland": "md", "massachusetts": "ma", "michigan": "mi",
                    "minnesota": "mn", "mississippi": "ms", "missouri": "mo",
                    "montana": "mt", "nebraska": "ne", "nevada": "nv",
                    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
                    "new york": "ny", "north carolina": "nc", "north dakota": "nd",
                    "ohio": "oh", "oklahoma": "ok", "oregon": "or",
                    "pennsylvania": "pa", "rhode island": "ri",
                    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn",
                    "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
                    "washington": "wa", "west virginia": "wv", "wisconsin": "wi",
                    "wyoming": "wy",
                }
                state_abbrev = _US_STATE_NAME_TO_CODE.get(second, second)
            # OWM legacy "city,state,country" only works for US.
            out.append(f"{first},{state_abbrev},US")
            out.append(f"{first},US")
        elif second in _COUNTRY_TO_TZ:
            # "Berlin, Germany" — OWM uses "city,country-code". We don't keep
            # ISO codes, so just try "city,country-name" and "city".
            out.append(first)
        else:
            # Unknown second chunk — try just the first.
            out.append(first)
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def _format_spoken_weather(
    *,
    city: str,
    actual: int,
    feels: int,
    desc: str,
    wind_mph: float | None = None,
    wind_dir: str = "",
) -> str:
    """One natural sentence for TTS — no API boilerplate."""
    desc_clean = (desc or "unclear").strip().lower()
    if feels != actual:
        head = f"It's {actual} degrees, feels like {feels}, and {desc_clean} in {city}."
    else:
        head = f"It's {actual} degrees and {desc_clean} in {city}."
    if isinstance(wind_mph, (int, float)) and float(wind_mph) >= 6:
        mph = round(float(wind_mph))
        if wind_dir:
            return head.rstrip(".") + f", with {mph} mile an hour winds from the {wind_dir}."
    return head


def _weather_search(
    query: str,
    api_key: str,
    *,
    context_location: str = "",
    fallback_timezone: str = "",
) -> str:
    """Get current weather using OpenWeatherMap.

    Tries multiple query variants ("Solon, Ohio" → "Solon,OH,US" → "Solon,US")
    so small US cities still resolve.
    """
    location = _extract_weather_location(query)
    if not location and _is_bare_weather_query(query):
        location = _weather_city_from_context(
            context_location=context_location,
            fallback_timezone=fallback_timezone,
        )
    if not location:
        return ""

    candidates = _expand_weather_query_locations(location)
    last_error: Exception | None = None
    for cand in candidates:
        encoded = urllib.parse.quote(cand)
        url = (
            "https://api.openweathermap.org/data/2.5/weather?"
            f"q={encoded}&appid={api_key}&units=imperial"
        )
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("cod") != 200:
                # 404 = not found; try next candidate.
                continue
            temp = round(data["main"]["feels_like"])
            actual = round(data["main"]["temp"])
            desc = data["weather"][0]["description"]
            city = data["name"]
            wind = data.get("wind") or {}
            wind_speed = wind.get("speed")
            wind_dir = _wind_direction(wind.get("deg"))
            spoken = _format_spoken_weather(
                city=city,
                actual=actual,
                feels=temp,
                desc=desc,
                wind_mph=float(wind_speed) if isinstance(wind_speed, (int, float)) else None,
                wind_dir=wind_dir,
            )
            return f"CURRENT_WEATHER: {spoken}"
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        log.warning("Weather fetch failed for %r: %s", location, last_error)
    else:
        log.info("Weather fetch found no match for %r (tried %d variants)", location, len(candidates))
    return ""


def _serper_search(query: str, api_key: str) -> str:
    """Search using Serper (Google) for news and current events."""
    url = "https://google.serper.dev/search"
    body = json.dumps({"q": query, "num": 3}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Answer box first
        answer_box = data.get("answerBox", {})
        if answer_box.get("answer"):
            return answer_box["answer"]
        if answer_box.get("snippet"):
            return answer_box["snippet"]
        # Top organic results
        organic = data.get("organic") or []
        parts = []
        for r in organic[:2]:
            snippet = (r.get("snippet") or "")[:200]
            source = r.get("title") or r.get("link") or ""
            try:
                if "/" in source:
                    source = source.split("/")[2].replace("www.", "")
            except Exception:
                pass
            if snippet:
                parts.append(f"{snippet} (Source: {source})" if source else snippet)
        return " ".join(parts).strip() or ""
    except Exception as exc:
        log.warning("Serper search failed: %s", exc)
        return ""


# Keywords that indicate real-time data is needed
_REALTIME_KEYWORDS = (
    "stock", "price", "market", "nasdaq", "dow", "s&p",
    "weather", "temperature", "forecast", "rain", "snow",
    "news", "latest", "today", "current", "right now", "happening",
    "score", "game", "match", "nfl", "nba", "mlb", "nhl",
    "crypto", "bitcoin", "ethereum",
    "who won", "election", "breaking",
    "trending", "popular", "release date",
)

# OpenWeatherMap-friendly capital when the user names only a country.
_COUNTRY_WEATHER_CAPITAL: dict[str, str] = {
    "india": "New Delhi,IN",
    "usa": "Washington,US",
    "united states": "Washington,US",
    "uk": "London,GB",
    "united kingdom": "London,GB",
    "china": "Beijing,CN",
    "japan": "Tokyo,JP",
    "australia": "Sydney,AU",
    "canada": "Toronto,CA",
    "germany": "Berlin,DE",
    "france": "Paris,FR",
    "brazil": "Sao Paulo,BR",
    "mexico": "Mexico City,MX",
    "spain": "Madrid,ES",
    "italy": "Rome,IT",
    "south korea": "Seoul,KR",
    "korea": "Seoul,KR",
}

# Jarvis-style lookup: find products, facts, or current info on the web.
_SEARCH_LOOKUP_KEYWORDS: tuple[str, ...] = (
    "look up", "lookup", "search for", "search the web", "web search", "google ",
    "research", "research the", "research that", "look into",
    "find me", "find some", "find a", "find the", "find out about", "find out",
    "pull that up", "pull it up", "pull up",
    "get me", "show me",
    "recommend", "recommendation", "recommendations",
    "where can i buy", "where to buy", "where do i buy",
    "how much is", "how much does", "how much are", "price of", "cost of",
    "what's popular", "whats popular", "most popular", "top rated", "top selling",
    "compare ", "versus ", " vs ",
    "what brand", "which brand", "who makes",
)

_VAGUE_SEARCH_RE = re.compile(
    r"\b("
    r"find\s+(?:me\s+|some\s+|a\s+|the\s+)?|"
    r"pull\s+(?:that\s+|it\s+)?up|"
    r"look\s+(?:that\s+|it\s+)?up|"
    r"search\s+for\s+that|"
    r"get\s+me\s+(?:some\s+|a\s+)?"
    r")\b",
    re.I,
)

_SEARCH_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "for", "me", "my", "some", "any", "that", "this",
    "it", "up", "can", "could", "you", "please", "would", "will", "just", "also",
    "find", "search", "look", "pull", "get", "show", "tell", "give", "help",
})

_WEATHER_KEYWORDS = ("weather", "temperature", "forecast", "rain", "snow", "hot", "cold", "humid")
_TIME_QUERY_KEYWORDS = (
    # Order matters: longer / more specific phrases first so the location
    # extractor doesn't slice off "right now" as part of the place name.
    "what time is it right now in",
    "what time is it currently in",
    "what's the time right now in",
    "whats the time right now in",
    "what is the time right now in",
    "what is the time currently in",
    # Inverted grammar variants — common in natural speech and STT output
    "what time it is right now in",
    "what time it is currently in",
    "what time it is in",
    "tell me what time it is in",
    "do you know what time it is in",
    "know what time it is in",
    "what time is it in",
    "what's the time in",
    "whats the time in",
    "what is the time in",
    "current local time in",
    "local time in",
    "current time in",
    "time right now in",
    "time currently in",
    "the time in",
    "time now in",
    "tell me the time in",
    "time in",
    "timezone in",
)

# Trailing temporal noise to strip from extracted location names.
_TIME_LOCATION_TRAILING_NOISE = (
    "right now",
    "currently",
    "at the moment",
    "right now",
    "rn",
    "now",
    "today",
    "tonight",
    "atm",
)

# When the user asks "what time is it" / "what's the time" with no location,
# fall back to the server's local timezone (rather than letting the LLM
# hallucinate). Override with KOOV_DEFAULT_TIMEZONE.
_BARE_TIME_PHRASES = (
    "what time is it",
    "what's the time",
    "whats the time",
    "what is the time",
    "what time it is",
    "current time",
    "tell me the time",
    "got the time",
    "know the time",
)

_BARE_WEATHER_PHRASES = (
    "what's the weather",
    "whats the weather",
    "what is the weather",
    "how's the weather",
    "hows the weather",
    "current weather",
    "weather right now",
    "tell me the weather",
)

# OpenWeatherMap city keys for bare "what's the weather?" (no place named).
_TZ_TO_WEATHER_CITY: dict[str, str] = {
    "America/New_York": "New York,US",
    "America/Chicago": "Chicago,US",
    "America/Denver": "Denver,US",
    "America/Los_Angeles": "Los Angeles,US",
    "America/Phoenix": "Phoenix,US",
    "America/Detroit": "Detroit,US",
    "America/Toronto": "Toronto,CA",
    "Europe/London": "London,GB",
    "Europe/Paris": "Paris,FR",
    "Europe/Berlin": "Berlin,DE",
    "Asia/Kolkata": "Mumbai,IN",
    "Asia/Tokyo": "Tokyo,JP",
    "Australia/Sydney": "Sydney,AU",
}


def _split_time_places(location: str) -> list[str]:
    """Split compound place lists: ``seoul and ohio``, ``tokyo, london``."""
    s = " ".join((location or "").split()).strip(" ?.!,")
    if not s:
        return []
    parts = re.split(r"\s+(?:and|&)\s+", s, flags=re.IGNORECASE)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        chunk = " ".join(part.split()).strip(" ?.!,")
        if not chunk:
            continue
        key = chunk.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
    return out


def _extract_time_locations(query: str) -> list[str]:
    """Return one or more place names from a time question."""
    single = _extract_time_location(query)
    if not single:
        return []
    return _split_time_places(single)


def _spoken_time_for_location(location: str) -> tuple[str, str, str] | None:
    """Resolve one place to (pretty_name, clock, tz_abbr) or None."""
    tz_name = _lookup_timezone(location)
    if not tz_name:
        return None
    now_local = _now_in_tz(tz_name)
    if now_local is None:
        return None
    stamp = now_local.strftime("%I:%M %p").lstrip("0")
    abbr = now_local.tzname() or ""
    pretty = _format_pretty_location(location) or location
    return pretty, stamp, abbr


def _format_spoken_time_answer(entries: list[tuple[str, str, str]]) -> str:
    """Natural spoken time answer for one or more cities."""
    if not entries:
        return ""
    # Spell out zone names — TTS/STT often clip "PDT" to "PD".
    _abbr_spoken = {
        "pdt": "Pacific",
        "pst": "Pacific",
        "edt": "Eastern",
        "est": "Eastern",
        "cdt": "Central",
        "cst": "Central",
        "mdt": "Mountain",
        "mst": "Mountain",
        "utc": "UTC",
        "gmt": "GMT",
    }
    parts: list[str] = []
    for pretty, stamp, abbr in entries:
        zone = _abbr_spoken.get((abbr or "").strip().lower(), (abbr or "").strip())
        suffix = f" {zone}" if zone else ""
        parts.append(f"in {pretty} it's {stamp}{suffix}")
    if len(parts) == 1:
        return f"{parts[0].capitalize()}."
    if len(parts) == 2:
        return f"{parts[0].capitalize()}, and {parts[1]}."
    return f"{', '.join(parts[:-1]).capitalize()}, and {parts[-1]}."


def _strip_time_location_noise(location: str) -> str:
    """Strip trailing temporal noise words from a time-location string.

    e.g. "mumbai right now" → "mumbai", "london rn" → "london".
    """
    s = " ".join((location or "").split()).strip(" ?.!,")
    if not s:
        return ""
    low = s.lower()
    for noise in _TIME_LOCATION_TRAILING_NOISE:
        if low.endswith(noise):
            s = s[: -len(noise)].strip(" ?.!,")
            low = s.lower()
    return s


def _extract_time_location(query: str) -> str:
    q = " ".join((query or "").strip().split()).lower()
    if not q:
        return ""

    # Try keyword phrases longest-first; whatever follows is the place name.
    for phrase in _TIME_QUERY_KEYWORDS:
        idx = q.find(phrase)
        if idx >= 0:
            tail = q[idx + len(phrase):].strip().rstrip("?.!,")
            tail = " ".join(tail.split())
            tail = _strip_time_location_noise(tail)
            if tail:
                return tail

    # Short forms: "tokyo time", "what's the time in tokyo" already handled above.
    if q.endswith(" time"):
        loc = q[:-5].strip().rstrip("?.!,")
        return _strip_time_location_noise(" ".join(loc.split()))

    # Regex fallback: catch "in <city>" from any time-flavoured question that
    # slipped through the keyword list (e.g. "do you know what time it is in
    # mumbai" where STT dropped words or reordered them).
    _time_flavour = ("time", "o'clock", "oclock", "hour")
    if any(w in q for w in _time_flavour):
        m = re.search(r"\bin\s+([a-z][a-z\s\-]{1,30}?)(?:\s*(?:right now|currently|rn|now|today|tonight|\?))?$", q)
        if m:
            candidate = _strip_time_location_noise(m.group(1).strip().rstrip("?.!,"))
            if candidate and _lookup_timezone(candidate):
                return candidate

    return ""


def _is_bare_weather_query(query: str) -> bool:
    """True when the user asked for weather with no city/region in the question."""
    q = " ".join((query or "").strip().split()).lower().rstrip("?.!,")
    if not q:
        return False
    if not any(k in q for k in _WEATHER_KEYWORDS):
        return False
    return not _weather_has_explicit_place(query)


def _weather_city_from_context(
    *,
    context_location: str = "",
    fallback_timezone: str = "",
) -> str:
    """Best-effort city for bare weather asks — same anchors as bare time queries."""
    ctx = (context_location or "").strip()
    if ctx and ctx.lower() not in _WEATHER_LOCATION_NOISE and len(ctx) > 2:
        if _lookup_timezone(ctx) or "," in ctx or len(ctx.split()) >= 2:
            return ctx
    tz = (fallback_timezone or "").strip()
    if tz in _TZ_TO_WEATHER_CITY:
        return _TZ_TO_WEATHER_CITY[tz]
    if tz:
        # America/New_York → try "new york" from IANA suffix.
        slug = tz.split("/")[-1].replace("_", " ").lower()
        if slug in _LOCATION_TO_TZ:
            return slug
    return ""


def _is_bare_time_query(query: str) -> bool:
    """True when the user asked "what time is it" / "what's the time" with no
    location — answer with the server's local timezone instead of letting the
    LLM make one up."""
    q = " ".join((query or "").strip().split()).lower().rstrip("?.!,")
    if not q:
        return False
    if any(phrase in q for phrase in _TIME_QUERY_KEYWORDS):
        return False  # has "in <location>" already
    return any(phrase in q for phrase in _BARE_TIME_PHRASES)


def _lookup_timezone(location: str) -> str:
    """Resolve a free-text place name to an IANA tz.

    Tries (in order): exact city, country fallback, US state fallback, then
    walks each comma-separated chunk so "pune, india" → "pune" → IST, and
    "solon, ohio" → "ohio" → America/New_York.
    """
    loc = (location or "").strip().lower().rstrip("?.!,")
    if not loc:
        return ""
    if loc in _LOCATION_TO_TZ:
        return _LOCATION_TO_TZ[loc]
    if loc in _COUNTRY_TO_TZ:
        return _COUNTRY_TO_TZ[loc]
    if loc in _US_STATE_TO_TZ:
        return _US_STATE_TO_TZ[loc]
    # Walk comma-separated chunks ("pune, india", "solon, ohio, usa")
    chunks = [c.strip() for c in loc.split(",") if c.strip()]
    for chunk in chunks:
        # Multiple words may also be a city or country (e.g. "new york").
        if chunk in _LOCATION_TO_TZ:
            return _LOCATION_TO_TZ[chunk]
        if chunk in _COUNTRY_TO_TZ:
            return _COUNTRY_TO_TZ[chunk]
        if chunk in _US_STATE_TO_TZ:
            return _US_STATE_TO_TZ[chunk]
    # Final pass: substring match for "city, region" where region appears
    # as the second/third chunk and the chunk happens to contain a known
    # city or country.
    for chunk in chunks:
        for k, v in _LOCATION_TO_TZ.items():
            if chunk == k or chunk.startswith(k + " ") or chunk.endswith(" " + k):
                return v
        for k, v in _COUNTRY_TO_TZ.items():
            if chunk == k or chunk.startswith(k + " ") or chunk.endswith(" " + k):
                return v
        for k, v in _US_STATE_TO_TZ.items():
            # Skip 2-letter abbrevs in substring matching ("in", "co", "or",
            # "ca") — they false-match common English words. Only do
            # substring matches for full state names.
            if len(k) <= 2:
                continue
            if chunk == k or chunk.startswith(k + " ") or chunk.endswith(" " + k):
                return v
    return ""


def _format_pretty_location(location: str) -> str:
    parts = [w.strip() for w in (location or "").split(",") if w.strip()]
    return ", ".join(" ".join(p.capitalize() for p in part.split()) for part in parts)


def _server_default_timezone() -> str:
    """Best-effort default timezone for "what time is it" with no place."""
    override = os.environ.get("KOOV_DEFAULT_TIMEZONE", "").strip()
    if override:
        return override
    try:
        return datetime.now().astimezone().tzinfo.tzname(None) or ""
    except Exception:
        return ""


def _time_search(
    query: str,
    *,
    context_location: str = "",
    fallback_timezone: str = "",
) -> str:
    locations = _extract_time_locations(query)
    if locations:
        entries: list[tuple[str, str, str]] = []
        for loc in locations:
            row = _spoken_time_for_location(loc)
            if row is not None:
                entries.append(row)
        if entries:
            spoken = _format_spoken_time_answer(entries)
            return f"CURRENT_TIME: {spoken}"
        # Location extracted but unrecognised (e.g. "know the" from "know
        # the time"). Fall through to bare-query path before giving up.
        log.info("Time query: no timezone match for locations=%r", locations)
    if _is_bare_time_query(query):
        ctx_loc = (context_location or "").strip()
        if ctx_loc:
            row = _spoken_time_for_location(ctx_loc)
            if row is not None:
                pretty, stamp, abbr = row
                suffix = f" {abbr}" if abbr else ""
                return f"CURRENT_TIME: In {pretty}, it's {stamp}{suffix}."
        fb_tz = (fallback_timezone or _server_default_timezone() or "").strip()
        if fb_tz:
            now_local = _now_in_tz(fb_tz)
            if now_local is not None:
                stamp = now_local.strftime("%I:%M %p").lstrip("0")
                abbr = now_local.tzname() or fb_tz
                suffix = f" {abbr}" if abbr else ""
                return f"CURRENT_TIME: It's {stamp}{suffix} here."
        # "What time is it?" — answer from the server's local clock so the
        # LLM doesn't make up a number.
        try:
            now_local = datetime.now().astimezone()
        except Exception:
            return ""
        stamp = now_local.strftime("%I:%M %p").lstrip("0")
        abbr = now_local.tzname() or _server_default_timezone() or ""
        suffix = f" ({abbr})" if abbr else ""
        return f"CURRENT_TIME: It's currently {stamp}{suffix} on this device's clock."
    return ""

_LOCATION_TO_TZ = {
    # ── North America (US) ──────────────────────────────────────────
    "new york": "America/New_York",
    "new york city": "America/New_York",
    "nyc": "America/New_York",
    "boston": "America/New_York",
    "philadelphia": "America/New_York",
    "washington": "America/New_York",
    "washington dc": "America/New_York",
    "miami": "America/New_York",
    "atlanta": "America/New_York",
    "orlando": "America/New_York",
    "tampa": "America/New_York",
    "charlotte": "America/New_York",
    "raleigh": "America/New_York",
    "pittsburgh": "America/New_York",
    "cleveland": "America/New_York",
    "cincinnati": "America/New_York",
    "solon": "America/New_York",
    "solon ohio": "America/New_York",
    "columbus": "America/New_York",
    "detroit": "America/Detroit",
    "indianapolis": "America/Indiana/Indianapolis",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "san jose": "America/Los_Angeles",
    "fremont": "America/Los_Angeles",
    "oakland": "America/Los_Angeles",
    "san diego": "America/Los_Angeles",
    "sacramento": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    "phoenix": "America/Phoenix",
    "denver": "America/Denver",
    "salt lake city": "America/Denver",
    "albuquerque": "America/Denver",
    "chicago": "America/Chicago",
    "minneapolis": "America/Chicago",
    "milwaukee": "America/Chicago",
    "st louis": "America/Chicago",
    "saint louis": "America/Chicago",
    "kansas city": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "austin": "America/Chicago",
    "san antonio": "America/Chicago",
    "new orleans": "America/Chicago",
    "honolulu": "Pacific/Honolulu",
    "anchorage": "America/Anchorage",
    # ── Canada ──────────────────────────────────────────────────────
    "toronto": "America/Toronto",
    "ottawa": "America/Toronto",
    "montreal": "America/Toronto",
    "quebec": "America/Toronto",
    "vancouver": "America/Vancouver",
    "calgary": "America/Edmonton",
    "edmonton": "America/Edmonton",
    "winnipeg": "America/Winnipeg",
    "halifax": "America/Halifax",
    # ── Latin America ───────────────────────────────────────────────
    "mexico city": "America/Mexico_City",
    "guadalajara": "America/Mexico_City",
    "monterrey": "America/Monterrey",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "sao paulo": "America/Sao_Paulo",
    "são paulo": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo",
    "lima": "America/Lima",
    "bogota": "America/Bogota",
    "santiago": "America/Santiago",
    # ── Europe ──────────────────────────────────────────────────────
    "london": "Europe/London",
    "manchester": "Europe/London",
    "edinburgh": "Europe/London",
    "dublin": "Europe/Dublin",
    "paris": "Europe/Paris",
    "lyon": "Europe/Paris",
    "marseille": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "munich": "Europe/Berlin",
    "frankfurt": "Europe/Berlin",
    "hamburg": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "rotterdam": "Europe/Amsterdam",
    "brussels": "Europe/Brussels",
    "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "lisbon": "Europe/Lisbon",
    "rome": "Europe/Rome",
    "milan": "Europe/Rome",
    "naples": "Europe/Rome",
    "vienna": "Europe/Vienna",
    "zurich": "Europe/Zurich",
    "geneva": "Europe/Zurich",
    "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo",
    "copenhagen": "Europe/Copenhagen",
    "helsinki": "Europe/Helsinki",
    "warsaw": "Europe/Warsaw",
    "prague": "Europe/Prague",
    "budapest": "Europe/Budapest",
    "athens": "Europe/Athens",
    "moscow": "Europe/Moscow",
    "saint petersburg": "Europe/Moscow",
    "istanbul": "Europe/Istanbul",
    # ── Middle East / Africa ────────────────────────────────────────
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "riyadh": "Asia/Riyadh",
    "doha": "Asia/Qatar",
    "tel aviv": "Asia/Jerusalem",
    "jerusalem": "Asia/Jerusalem",
    "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos",
    "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg",
    "nairobi": "Africa/Nairobi",
    # ── South Asia ──────────────────────────────────────────────────
    "mumbai": "Asia/Kolkata",
    "bombay": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata",
    "calcutta": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "madras": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    "ahmedabad": "Asia/Kolkata",
    "jaipur": "Asia/Kolkata",
    "lucknow": "Asia/Kolkata",
    "kanpur": "Asia/Kolkata",
    "nagpur": "Asia/Kolkata",
    "surat": "Asia/Kolkata",
    "indore": "Asia/Kolkata",
    "bhopal": "Asia/Kolkata",
    "patna": "Asia/Kolkata",
    "vadodara": "Asia/Kolkata",
    "coimbatore": "Asia/Kolkata",
    "kochi": "Asia/Kolkata",
    "thiruvananthapuram": "Asia/Kolkata",
    "trivandrum": "Asia/Kolkata",
    "goa": "Asia/Kolkata",
    "karachi": "Asia/Karachi",
    "lahore": "Asia/Karachi",
    "islamabad": "Asia/Karachi",
    "dhaka": "Asia/Dhaka",
    "colombo": "Asia/Colombo",
    "kathmandu": "Asia/Kathmandu",
    # ── East / Southeast Asia ───────────────────────────────────────
    "singapore": "Asia/Singapore",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "bangkok": "Asia/Bangkok",
    "hanoi": "Asia/Ho_Chi_Minh",
    "ho chi minh": "Asia/Ho_Chi_Minh",
    "jakarta": "Asia/Jakarta",
    "manila": "Asia/Manila",
    "tokyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "kyoto": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "busan": "Asia/Seoul",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai",
    "guangzhou": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    # ── Oceania ─────────────────────────────────────────────────────
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    "adelaide": "Australia/Adelaide",
    "auckland": "Pacific/Auckland",
    "wellington": "Pacific/Auckland",
    "christchurch": "Pacific/Auckland",
}

# Country-level fallbacks: "what time is it in india" → IST. Used when no city
# match is found. Mapped to a representative IANA zone.
_COUNTRY_TO_TZ = {
    "india": "Asia/Kolkata",
    "pakistan": "Asia/Karachi",
    "bangladesh": "Asia/Dhaka",
    "sri lanka": "Asia/Colombo",
    "nepal": "Asia/Kathmandu",
    "uk": "Europe/London",
    "united kingdom": "Europe/London",
    "england": "Europe/London",
    "scotland": "Europe/London",
    "ireland": "Europe/Dublin",
    "france": "Europe/Paris",
    "germany": "Europe/Berlin",
    "spain": "Europe/Madrid",
    "italy": "Europe/Rome",
    "netherlands": "Europe/Amsterdam",
    "belgium": "Europe/Brussels",
    "switzerland": "Europe/Zurich",
    "sweden": "Europe/Stockholm",
    "norway": "Europe/Oslo",
    "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki",
    "poland": "Europe/Warsaw",
    "russia": "Europe/Moscow",
    "turkey": "Europe/Istanbul",
    "uae": "Asia/Dubai",
    "saudi arabia": "Asia/Riyadh",
    "israel": "Asia/Jerusalem",
    "egypt": "Africa/Cairo",
    "south africa": "Africa/Johannesburg",
    "kenya": "Africa/Nairobi",
    "nigeria": "Africa/Lagos",
    "japan": "Asia/Tokyo",
    "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul",
    "china": "Asia/Shanghai",
    "taiwan": "Asia/Taipei",
    "singapore": "Asia/Singapore",
    "thailand": "Asia/Bangkok",
    "vietnam": "Asia/Ho_Chi_Minh",
    "philippines": "Asia/Manila",
    "indonesia": "Asia/Jakarta",
    "malaysia": "Asia/Kuala_Lumpur",
    "australia": "Australia/Sydney",
    "new zealand": "Pacific/Auckland",
    "canada": "America/Toronto",
    "mexico": "America/Mexico_City",
    "brazil": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires",
    "chile": "America/Santiago",
    "peru": "America/Lima",
    "colombia": "America/Bogota",
}

# US state aliases (only ones common in everyday speech). Used for both time
# (default to ET unless explicitly listed) and weather country-code fallback.
_US_STATE_TO_TZ = {
    "ohio": "America/New_York",
    "oh": "America/New_York",
    "new york": "America/New_York",
    "ny": "America/New_York",
    "florida": "America/New_York",
    "fl": "America/New_York",
    "georgia": "America/New_York",
    "ga": "America/New_York",
    "michigan": "America/Detroit",
    "mi": "America/Detroit",
    "indiana": "America/Indiana/Indianapolis",
    "in": "America/Indiana/Indianapolis",
    "illinois": "America/Chicago",
    "il": "America/Chicago",
    "texas": "America/Chicago",
    "tx": "America/Chicago",
    "minnesota": "America/Chicago",
    "mn": "America/Chicago",
    "missouri": "America/Chicago",
    "mo": "America/Chicago",
    "louisiana": "America/Chicago",
    "la": "America/Chicago",
    "arizona": "America/Phoenix",
    "az": "America/Phoenix",
    "colorado": "America/Denver",
    "co": "America/Denver",
    "utah": "America/Denver",
    "ut": "America/Denver",
    "new mexico": "America/Denver",
    "nm": "America/Denver",
    "california": "America/Los_Angeles",
    "ca": "America/Los_Angeles",
    "oregon": "America/Los_Angeles",
    "or": "America/Los_Angeles",
    "washington": "America/Los_Angeles",
    "wa": "America/Los_Angeles",
    "nevada": "America/Los_Angeles",
    "nv": "America/Los_Angeles",
    "alaska": "America/Anchorage",
    "ak": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "hi": "Pacific/Honolulu",
}


def _search_topic_words(text: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", (text or "").lower())
        if w not in _SEARCH_STOPWORDS and len(w) > 1
    ]


# Actionable live-data asks — never inherit a prior dialog line as the search topic.
_DIRECT_REALTIME_LOOKUP_MARKERS: tuple[str, ...] = (
    "who won",
    "who's winning",
    "whos winning",
    "score",
    "finals",
    "playoff",
    "playoffs",
    "game today",
    "game tonight",
    "match today",
    "weather",
    "temperature",
    "forecast",
    "stock",
    "price",
    "headline",
    "breaking",
    "right now",
    "today",
    "tonight",
    "currently",
    "latest",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "world series",
    "super bowl",
)


def _is_direct_realtime_lookup(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(m in t for m in _DIRECT_REALTIME_LOOKUP_MARKERS)


def is_vague_search_request(text: str) -> bool:
    """True when the user wants a lookup but did not name the topic (needs dialog context)."""
    t = (text or "").strip()
    if not t:
        return False
    if _is_direct_realtime_lookup(t):
        return False
    words = _search_topic_words(t)
    if len(words) >= 4:
        return False
    if _VAGUE_SEARCH_RE.search(t):
        return len(words) <= 2
    return len(words) <= 2


def _with_search_freshness(query: str) -> str:
    """Append today's date so sports/news searches return current results, not last season."""
    q = (query or "").strip()
    if not q:
        return q
    if not _is_direct_realtime_lookup(q):
        return q[:200]
    low = q.lower()
    year = str(datetime.now().year)
    if year in q:
        return q[:200]
    month_day = datetime.now().strftime("%B %d %Y")
    if month_day.lower() in low:
        return q[:200]
    return f"{q} {month_day}"[:200]


def needs_web_lookup(text: str) -> bool:
    """Return True when Koov should search the web (products, facts, recommendations)."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(k in t for k in _SEARCH_LOOKUP_KEYWORDS):
        return True
    if _VAGUE_SEARCH_RE.search(text or ""):
        return True
    if t.startswith(("what is ", "who is ", "define ")):
        return True
    return False


def build_search_query(
    query: str,
    *,
    last_user: str | None = None,
    last_koov: str | None = None,
) -> str:
    """
    Build a web-search string. Vague continuations ('find some for me') inherit the
    last Koov thread topic from dialog memory.
    """
    q = (query or "").strip()
    if not q:
        return ""

    if not is_vague_search_request(q):
        return _with_search_freshness(q)

    lu = (last_user or "").strip()
    lk = (last_koov or "").strip()
    topic = ""
    if lu and len(_search_topic_words(lu)) >= 2:
        topic = lu[:160]
    elif lk:
        try:
            from koov.dialog_memory import extract_salient_terms

            terms = extract_salient_terms(lk, max_terms=6)
            if terms:
                topic = " ".join(terms)[:160]
        except Exception:
            topic = lk[:120]

    if topic:
        low = q.lower()
        if any(p in low for p in ("find", "search", "pull", "look", "get me", "show")):
            return f"{topic} recommendations"[:200]
        return _with_search_freshness(f"{topic} {q}")
    return _with_search_freshness(q)


def query_is_fast_deterministic_realtime(query: str) -> bool:
    """True when Koov can answer time/weather without LLM latency."""
    q = (query or "").strip()
    if not q:
        return False
    low = q.lower()
    if _is_bare_time_query(q) or bool(_extract_time_locations(q)):
        return True
    if any(k in low for k in _WEATHER_KEYWORDS):
        if _is_bare_weather_query(q) or bool(_extract_weather_location(q)):
            return bool(os.environ.get("KOOV_WEATHER_API_KEY", "").strip())
    return False


def fetch_deterministic_spoken_answer(
    query: str,
    *,
    context_location: str = "",
    fallback_timezone: str = "",
) -> str:
    """Return a natural spoken answer for time/weather, or empty when unavailable."""
    time_result = _time_search(
        query,
        context_location=context_location,
        fallback_timezone=fallback_timezone,
    )
    if time_result:
        return time_result.split(":", 1)[1].strip()
    low = (query or "").lower()
    weather_key = os.environ.get("KOOV_WEATHER_API_KEY", "").strip()
    if weather_key and any(k in low for k in _WEATHER_KEYWORDS):
        weather_result = _weather_search(
            query,
            weather_key,
            context_location=context_location,
            fallback_timezone=fallback_timezone,
        )
        if weather_result:
            return weather_result.split(":", 1)[1].strip()
    return ""


def needs_realtime(text: str) -> bool:
    """Return True if the question likely needs real-time data."""
    t = text.lower()
    if any(k in t for k in _REALTIME_KEYWORDS):
        return True
    if any(k in t for k in _TIME_QUERY_KEYWORDS):
        return True
    if any(p in t for p in _BARE_TIME_PHRASES):
        return True
    if needs_web_lookup(text):
        return True
    return False


def fetch_realtime(
    query: str,
    *,
    last_user: str | None = None,
    last_koov: str | None = None,
    context_location: str = "",
    fallback_timezone: str = "",
) -> str:
    """
    Fetch real-time data for a query.
    Routes to weather, search, or news based on query type.
    Returns a short string to inject into the LLM prompt.
    """
    tavily_key = os.environ.get("KOOV_TAVILY_API_KEY", "").strip()
    weather_key = os.environ.get("KOOV_WEATHER_API_KEY", "").strip()
    serper_key = os.environ.get("KOOV_SERPER_API_KEY", "").strip()

    q = query.lower()
    search_q = build_search_query(query, last_user=last_user, last_koov=last_koov)

    # Deterministic timezone query path (fast and no external API dependency).
    time_result = _time_search(
        query,
        context_location=context_location,
        fallback_timezone=fallback_timezone,
    )
    if time_result:
        return time_result

    # Weather query — bare "what's the weather?" uses meeting/user timezone city.
    if any(k in q for k in _WEATHER_KEYWORDS) and weather_key:
        result = _weather_search(
            query,
            weather_key,
            context_location=context_location,
            fallback_timezone=fallback_timezone,
        )
        if result:
            return result

    _max_results = 5 if needs_web_lookup(query) else 3

    # Try Tavily first (best for general questions)
    if tavily_key:
        result = _tavily_search(search_q, tavily_key, max_results=_max_results)
        if result:
            return result

    # Fall back to Serper
    if serper_key:
        result = _serper_search(search_q, serper_key)
        if result:
            return result

    # No API keys — DuckDuckGo instant answers (free, no key).
    try:
        from koov.web_search import web_search_snippet

        ddg = web_search_snippet(search_q, max_chars=550)
        if ddg:
            return ddg
    except Exception as exc:
        log.debug("DuckDuckGo fallback failed: %s", exc)

    return ""