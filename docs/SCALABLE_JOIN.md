# Scalable Join: No Browser, No Tabs

To scale Koov (many meetings, server deployment, no visible browser/tabs), you need a way to join meetings **without** opening Chrome. This doc describes the options.

---

## 1. External audio (recommended for scale)

**Idea:** Koov does **not** open any browser. Meeting audio is brought to Koov by some other means (another service that joins via Zoom SDK, or you join on another device and route audio). Koov just listens on its configured input device and speaks on its output device.

**How to use**

- **Dashboard:** When starting a session, send `"join_backend": "external"` (or set env `KOOV_JOIN_BACKEND=external` so all joins use it):
  ```json
  POST /api/join-meeting
  { "meeting_link": "https://zoom.us/j/...", "user_id": "user-1", "join_backend": "external" }
  ```
- **Server:** Does **not** open a browser. It starts the Koov agent with your normal audio config (`KOOV_INPUT_DEVICE`, `KOOV_OUTPUT_DEVICE`). The agent listens and takes notes as soon as audio is present.

**Your responsibility**

- Get meeting audio **into** Koov’s input device (and optionally send Koov’s TTS **out** to the meeting):
  - **Option A:** Run a **Zoom Meeting SDK** bot (e.g. on Linux/Docker) that joins the meeting and forwards audio to this machine (e.g. via virtual sound device or stream). Koov reads from that device.
  - **Option B:** Join the meeting yourself on another device (phone, laptop), set that device’s output to stream to Koov’s host (e.g. virtual cable / BlackHole on the Koov server), and set Koov’s input to that device.
  - **Option C:** Use a meeting platform that exposes a bot/headless API (e.g. LiveKit, Daily) and pipe that audio into Koov’s input.

**Benefits**

- No browser, no tabs, no Selenium/Chrome on the Koov process.
- You can run many Koov instances; each gets audio from its own source.
- Works on any OS (macOS, Linux, Docker) as long as audio is routed to Koov’s input.

---

## 2. Zoom Meeting SDK (no browser, Zoom only) — implemented

For **Zoom** meetings Koov can join with **Zoom Meeting SDK** and get/send audio with **no browser**.

- **How:** Set `join_backend=zoom_sdk` and send a Zoom meeting link. On Linux (with `zoom-meeting-sdk` and PyJWT installed), the server joins via the SDK, receives raw audio, and feeds it into Koov; TTS is sent back into the meeting.
- **Requirements:** Linux, Zoom Meeting SDK app (Client ID / Client Secret in `.env`), `pip install -r requirements-zoom.txt`. See [ZOOM_SDK_SETUP](ZOOM_SDK_SETUP.md).

**API**

```http
POST /api/join-meeting
{ "meeting_link": "https://zoom.us/j/123456789?pwd=...", "user_id": "user-1", "join_backend": "zoom_sdk" }
```

The host may need to **allow recording** when Zoom prompts so Koov can receive and send audio.

---

## 3. Google Meet

Google Meet has no official “join as bot” API for consumer links. Options:

- **Browser (current):** Use Koov with `join_backend=browser` (default). Not scalable (one tab per meeting, visible or headless, and headless has no audio).
- **External:** Use **external** join and bring Meet audio to Koov via a third-party bridge or by joining Meet on another device and routing audio to Koov.
- **Other platforms:** Use Meet via a platform that supports headless participants (e.g. LiveKit, Daily) if you integrate with them.

---

## 4. Summary

| Goal                     | Use                          | Browser/tabs |
|--------------------------|------------------------------|---------------|
| Scale, any meeting       | `join_backend=external` + pipe audio into Koov | No            |
| Scale, Zoom only (Linux) | `join_backend=zoom_sdk` + Zoom link + ZOOM_APP_CLIENT_ID/SECRET | No            |
| Single meeting, simple   | `join_backend=browser` (default), optionally minimize window | Yes (one tab) |

**Practical path for scale today:** Use **external** join. Run one or more “meeting bridge” services (e.g. Zoom SDK on Linux) that join meetings and send audio to each Koov instance’s input device; dashboard calls `POST /api/join-meeting` with `"join_backend": "external"` so Koov never opens a browser.

---

## 5. API quick reference

**Request (external join, no browser):**

```http
POST /api/join-meeting
Content-Type: application/json

{
  "meeting_link": "https://zoom.us/j/123456789?pwd=...",
  "user_id": "user-123",
  "join_backend": "external"
}
```

**Environment (default all joins to external):**

```bash
KOOV_JOIN_BACKEND=external
```

If `join_backend` is omitted, the server uses `KOOV_JOIN_BACKEND` if set, otherwise `"browser"`.

- **zoom_sdk:** Zoom only, Linux only. No browser; full audio via Zoom Meeting SDK. See [ZOOM_SDK_SETUP](ZOOM_SDK_SETUP.md).
