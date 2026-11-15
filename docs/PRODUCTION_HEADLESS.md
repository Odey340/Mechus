# Production Headless: Koov Without Manual Host Actions

When running Koov in production (Docker, cloud, headless Linux), you cannot rely on the host to approve recording or unmute the bot. This doc describes production-grade paths to make Koov fully automated and reliable.

---

## The Problem

In headless/Docker mode, Koov faces:

1. **Host must allow recording** — Zoom prompts the host; if they don't respond, Koov gets no audio (SDKERR_NO_PERMISSION).
2. **Mute on entry** — Zoom can mute new participants; the host must unmute Koov for TTS to be heard.
3. **Raw audio callbacks unreliable** — On headless Linux/Docker, Zoom's raw audio callbacks sometimes never fire even when subscribe succeeds.

---

## Production Paths

### Path 1: Recording Join Token (Recommended for Zoom SDK)

**Eliminates the host recording prompt.** Your backend obtains a local recording token from Zoom and passes it when Koov joins. Koov then gets recording privilege automatically—no host action.

**Note:** Recording tokens solve **permission** only. They do not improve audio reliability or latency; those depend on running on real Linux and a solid audio path. See [IMPLEMENTATION_PRIORITY.md](IMPLEMENTATION_PRIORITY.md).

**Requirements:**
- Your backend creates or owns the meetings (or the meeting host has OAuth-authorized your app).
- Zoom OAuth app with scopes: `meeting:read:local_recording_token` (and `meeting:read:local_recording_token:admin` if admin).
- Host's account has local recording enabled and allows participants to record locally.

**Flow:**
1. User schedules a meeting (or you create it via Zoom API). Meeting is owned by a Zoom user.
2. That user authorizes your Zoom OAuth app (one-time).
3. When a user wants Koov to join: your backend uses the host's access token to call:
   ```
   GET https://api.zoom.us/v2/meetings/{meetingId}/jointoken/local_recording?bypass_waiting_room=true
   Authorization: Bearer {host_access_token}
   ```
4. Pass the returned token to Koov's join API: `POST /api/join-meeting` with `recording_token` in the body.
5. Koov joins with the token → recording and waiting-room bypass are pre-granted.

**Implementation:**

1. **Backend:** When a user wants Koov to join, your backend calls Zoom:
   ```
   GET https://api.zoom.us/v2/meetings/{meetingId}/jointoken/local_recording?bypass_waiting_room=true
   Authorization: Bearer {host_access_token}
   ```
   Requires OAuth scopes: `meeting:read:local_recording_token` (and `meeting:read:local_recording_token:admin` if admin). The token expires in 120 seconds—fetch just-in-time.

2. **Koov:** Pass the token in `POST /api/join-meeting`:
   ```json
   {
     "meeting_link": "https://zoom.us/j/123456789",
     "recording_token": "<token from step 1>",
     "join_backend": "zoom_sdk"
   }
   ```

---

### Path 2: Meeting Creation with Correct Settings

**Reduces mute-on-entry.** When your backend creates meetings via Zoom API, set:

- `mute_upon_entry: false` — Koov won't join muted; TTS will be heard without host unmuting.
- Meeting options: ensure local recording is allowed if possible (account-level).

**Example (Zoom API):**
```json
POST /users/{userId}/meetings
{
  "topic": "Meeting with Koov",
  "type": 2,
  "settings": {
    "mute_upon_entry": false,
    "join_before_host": true
  }
}
```

Combine with Path 1 (recording token) for full automation when you create the meetings.

---

### Path 3: Recall.ai or Meeting-Bot API

**Bypasses Zoom SDK entirely.** Use a third-party API (e.g. [Recall.ai](https://recall.ai)) that joins Zoom/Meet/Teams as a bot and streams real-time audio to you.

**Pros:**
- No host prompts, no mute-on-entry, no raw audio callback issues.
- Multi-platform (Zoom, Meet, Teams) with one integration.
- Production SLA, scaling, and reliability handled by the provider.

**Cons:**
- Paid per meeting-minute.
- Koov needs an adapter to consume their stream (WebSocket or webhooks) instead of `ZoomAudioInput`.

**When to use:** Ship fast, many meetings, minimal infra. You own wake-word, STT, LLM, TTS; they own join + audio delivery.

---

### Path 4: Zoom RTMS (Realtime Media Streams)

**For Zoom Apps.** If you're building a Zoom App (users install your app and start meetings with it), Zoom RTMS provides real-time audio + transcript over WebSockets.

**Pros:** Zoom-native, real-time, no host prompts, no mute-on-entry.
**Cons:** Only works when the meeting host has your Zoom App installed. Receive-only—Koov cannot speak back into the meeting via RTMS (use Meeting SDK or Recall for TTS).

**Try it:** Run `python scripts/rtms_koov.py` and configure your Zoom App webhook. See **docs/RTMS_SETUP.md**.

---

## Summary

| Scenario | Path |
|----------|------|
| You create meetings, want Zoom SDK | **Path 1** (recording token) + **Path 2** (mute_upon_entry: false) |
| Join any meeting link, need reliability | **Path 3** (Recall.ai or similar) |
| Zoom App with in-meeting assistant | **Path 4** (RTMS) |
| Own infra, meetings you create | **Path 1 + 2**; run on Linux host (not Docker-on-Mac) for best raw audio callback reliability |

---

## Infrastructure Notes

- **Linux host:** Run Koov on a real Linux host (VM, EC2, etc.) instead of Docker-on-Mac. Raw audio callbacks are more reliable on Linux.
- **PulseAudio:** Koov's Zoom image already uses PulseAudio + ALSA null sink. If callbacks still don't fire, try the [Zoom Meeting SDK Headless Linux Sample](https://github.com/zoom/meetingsdk-headless-linux-sample) to validate your environment.
- **Scaling:** One worker per meeting when using Zoom SDK (in-memory sessions). Scale horizontally behind a load balancer with sticky sessions.
