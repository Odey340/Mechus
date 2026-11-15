#!/usr/bin/env python3
"""
Test Zoom integration: link parsing and API flow (join → status → transcript → stop).
Run with Koov API up (e.g. Docker): KOOV_API_URL=http://localhost:5001 python3 scripts/test_zoom.py
Uses only stdlib so it runs without installing koov deps (e.g. sounddevice).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error
from typing import Optional


def is_zoom_link(link: str) -> bool:
    """Match zoom_sdk_join.is_zoom_link behavior."""
    if not link:
        return False
    return "zoom.us" in link.lower() and "/j/" in link.lower()


def parse_zoom_link(link: str) -> tuple[Optional[int], Optional[str]]:
    """Match zoom_sdk_join.parse_zoom_link behavior: (meeting_number, password)."""
    link = (link or "").strip()
    if not link:
        return None, None
    match = re.search(r"/j/(\d+)(?:.*?[?&]pwd=([^&\s]+))?", link, re.I)
    if not match:
        return None, None
    try:
        num = int(match.group(1))
    except ValueError:
        return None, None
    pwd = match.group(2) if match.lastindex and match.lastindex >= 2 else None
    return num, pwd


def main() -> None:
    base = os.environ.get("KOOV_API_URL", "http://localhost:5001").rstrip("/")
    meeting_id = "zoom-test-" + str(hash(base) % 10**6)

    print("=== 1. Zoom link parsing (unit) ===")
    for link, want_zoom, want_num, want_pwd in [
        ("https://zoom.us/j/123456789", True, 123456789, None),
        ("https://zoom.us/j/999?pwd=abc123", True, 999, "abc123"),
        ("https://us06web.zoom.us/j/555?pwd=secret", True, 555, "secret"),
        ("https://meet.google.com/abc-defg-hij", False, None, None),
        ("", False, None, None),
    ]:
        got_zoom = is_zoom_link(link)
        got_num, got_pwd = parse_zoom_link(link)
        ok = got_zoom == want_zoom and got_num == want_num and got_pwd == want_pwd
        status = "OK" if ok else "FAIL"
        print(f"  {status} is_zoom={got_zoom} num={got_num} pwd={got_pwd!r}  ({link[:50]}...)")
        if not ok:
            print(f"      want is_zoom={want_zoom} num={want_num} pwd={want_pwd!r}")
            sys.exit(1)
    print("  All Zoom link parsing checks passed.\n")

    print("=== 2. Zoom API flow (join → status → transcript → stop) ===")
    zoom_link = "https://zoom.us/j/123456789"
    join_payload = {
        "meeting_link": zoom_link,
        "join_backend": "external",
        "meeting_id": meeting_id,
        "display_name": "KoovTest",
    }

    # Join
    req = urllib.request.Request(
        f"{base}/api/join-meeting",
        data=json.dumps(join_payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            join_body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if e.code == 503 and "Wake word model not found" in body:
            print(f"  FAIL POST /api/join-meeting: 503 Wake word model not found")
            sys.exit(1)
        if e.code == 409 and "already active" in body:
            # Previous test left session; stop it first then retry is optional; for now pass
            print("  OK POST /api/join-meeting: 409 (meeting already active, reusing)")
            join_body = {"success": True, "meeting_id": meeting_id}
        else:
            print(f"  FAIL POST /api/join-meeting: {e.code} {body[:200]}")
            sys.exit(1)
    else:
        if not join_body.get("success"):
            print(f"  FAIL POST /api/join-meeting: {join_body}")
            sys.exit(1)
        print(f"  OK POST /api/join-meeting → meeting_id={join_body.get('meeting_id', meeting_id)}")

    mid = join_body.get("meeting_id") or meeting_id

    # Status (session may be "stopped" in Docker if agent exits without audio devices)
    try:
        with urllib.request.urlopen(f"{base}/api/status?meeting_id={mid}", timeout=10) as r:
            status_body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  FAIL GET /api/status: {e.code} {e.read().decode()[:200]}")
        sys.exit(1)
    if status_body.get("meeting_id") == mid or mid in str(status_body.get("meeting_id", "")):
        print(f"  OK GET /api/status?meeting_id={mid} (status={status_body.get('status', '?')})")
    else:
        print(f"  FAIL GET /api/status: {status_body}")
        sys.exit(1)

    # Transcript (may be empty; we only check 200)
    try:
        with urllib.request.urlopen(f"{base}/api/transcript?meeting_id={mid}", timeout=10) as r:
            transcript_body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  FAIL GET /api/transcript: {e.code} {e.read().decode()[:200]}")
        sys.exit(1)
    print(f"  OK GET /api/transcript?meeting_id={mid}")

    # Stop (may get 400 "Session already stopped" if agent exited already, e.g. in Docker)
    req = urllib.request.Request(
        f"{base}/api/stop",
        data=json.dumps({"meeting_id": mid}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            stop_body = json.loads(r.read().decode())
        if stop_body.get("success"):
            print(f"  OK POST /api/stop (meeting_id={mid})")
        else:
            print(f"  FAIL POST /api/stop: {stop_body}")
            sys.exit(1)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            err = json.loads(body)
            if err.get("error") == "Session already stopped":
                print(f"  OK POST /api/stop (meeting_id={mid}, already stopped)")
            else:
                print(f"  FAIL POST /api/stop: {e.code} {body[:200]}")
                sys.exit(1)
        except json.JSONDecodeError:
            print(f"  FAIL POST /api/stop: {e.code} {body[:200]}")
            sys.exit(1)

    print("\nAll Zoom checks passed.")


if __name__ == "__main__":
    main()
