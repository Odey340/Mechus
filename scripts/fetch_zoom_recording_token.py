#!/usr/bin/env python3
"""
Fetch Zoom local recording join token for a meeting.

Use this to get a recording_token to pass to Koov's POST /api/join-meeting so the bot
can receive raw audio without the host clicking "Allow recording."

Requirements:
  - ZOOM_OAUTH_ACCESS_TOKEN: Zoom OAuth access token (user or server) with scope
    meeting:read:local_recording_token (and meeting:read:local_recording_token:admin if
    the meeting belongs to another user).
  - Meeting ID: numeric ID from the Zoom link (e.g. from https://zoom.us/j/1234567890
    the ID is 1234567890). You can pass the full link and the script will parse it.

Usage:
  export ZOOM_OAUTH_ACCESS_TOKEN="your_oauth_access_token"
  python scripts/fetch_zoom_recording_token.py 1234567890
  # or
  python scripts/fetch_zoom_recording_token.py "https://zoom.us/j/1234567890?pwd=..."

Token expires in ~120 seconds; use it immediately in your join request.
"""
from __future__ import annotations

import json
import os
import sys

# Reuse Koov's Zoom link parser so we accept link or numeric ID
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from koov.zoom_sdk_join import parse_zoom_link


def main() -> None:
    token = (os.getenv("ZOOM_OAUTH_ACCESS_TOKEN") or "").strip()
    if not token:
        print("Error: set ZOOM_OAUTH_ACCESS_TOKEN", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: fetch_zoom_recording_token.py <meeting_id_or_zoom_link>", file=sys.stderr)
        sys.exit(1)

    raw = sys.argv[1].strip()
    meeting_number, _ = parse_zoom_link(raw)
    if meeting_number is None:
        # Try as plain numeric ID
        try:
            meeting_number = int(raw)
        except ValueError:
            print("Error: could not parse meeting ID or Zoom link", file=sys.stderr)
            sys.exit(1)

    url = f"https://api.zoom.us/v2/meetings/{meeting_number}/jointoken/local_recording?bypass_waiting_room=true"
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode()
    except Exception as e:
        print(f"Error calling Zoom API: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        out = json.loads(data)
        # Zoom returns {"token": "..."}
        rec_token = (out.get("token") or "").strip()
        if not rec_token:
            print("Error: Zoom response had no 'token'", file=sys.stderr)
            sys.exit(1)
        print(rec_token)
    except json.JSONDecodeError as e:
        print(f"Error parsing Zoom response: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
