# Getting Meeting Audio Without RTMS (First-Party Zoom Only)

If you don't want RTMS and **no third-party services** (no Recall.ai, Symbl, etc.), the only production option is the **Zoom Meeting SDK** with optional **recording token**. All of this is first-party Zoom; Koov already supports it.

---

## 1. Zoom Meeting SDK + Raw Recording (already implemented)

**What it is:** Koov joins the meeting as a bot via the Zoom Meeting SDK. After the host allows recording once (or you use a recording token so they don't have to), Zoom sends raw mixed audio (PCM) via `onMixedAudioRawDataReceived`. Koov uses this for STT, wake word, and context. The same bot speaks via Zoom's virtual mic (TTS).

**Pros:**
- Already implemented in `koov/zoom_sdk_join.py`.
- No third party; only Zoom + your app.
- Single bot: listen + speak in one join.

**Cons:**
- Host must allow recording when Zoom prompts — **unless** you use a recording token (see below).
- Raw audio callbacks can be unreliable in headless Docker on Mac (emulation); works best on **native Linux**.
- Zoom only (no Google Meet / Teams from this path).

**Best for:** Production Zoom meetings when you run on **Linux** and use a **recording token** so the host never sees a prompt.

---

## 2. Zoom Recording Token (no host prompt, first-party)

**What it is:** Your backend gets a **local recording join token** from Zoom's own REST API and passes it in the Meeting SDK join params. The bot then has recording privilege **without** the host seeing "Allow recording?". This is Zoom's API, not a third-party service.

**Flow:**
1. Your backend calls Zoom:  
   `GET https://api.zoom.us/v2/meetings/{meetingId}/jointoken/local_recording?bypass_waiting_room=true`  
   with the **host's** OAuth access token in `Authorization: Bearer {token}`.
2. You pass the returned token to Koov: `POST /api/join-meeting` with `"recording_token": "<token>"` in the body.
3. Koov joins with that token; recording is pre-granted, no host prompt.

**Requirements:**
- A **Zoom OAuth app** (separate from or in addition to your Meeting SDK app) with scopes:  
  `meeting:read:local_recording_token` (and `meeting:read:local_recording_token:admin` if admin).
- Meetings created/owned by users who have authorized your OAuth app (or you create meetings via Zoom API).
- Token is short-lived (~120 s); fetch it **just before** join.

**Implementation:** Koov already accepts `recording_token` in the join API. You only need to implement the **dashboard/backend** step that fetches the token from Zoom and sends it in the join request. Full details: [PRODUCTION_HEADLESS.md](PRODUCTION_HEADLESS.md).

**Best for:** Production Zoom-only, no third party, no host prompt.

---

## 3. Browser / Virtual Device (dev only)

**What it is:** The user joins the meeting in a browser or Zoom app. System audio (or a virtual loopback like BlackHole) is used as Koov's input device. No bot; Koov runs on the user's machine.

**Pros:** Works on Mac/Windows without Zoom SDK; no host prompt; quick for local dev.

**Cons:** Not scalable; not "Koov joins on its own"; each user must run Koov and configure audio.

**Best for:** Local development and demos only. Not production.

---

## Summary (no third party)

| Goal | What to do | Effort |
|------|------------|--------|
| **Production Zoom, no RTMS, no third party** | Use **Zoom Meeting SDK** (already in Koov) + **recording token** from Zoom's API so the host never sees "Allow recording?" | Low: implement token fetch in your backend; pass token in `POST /api/join-meeting`. |
| **Reliability** | Run the Zoom SDK stack on **native Linux** (not Docker on Mac); use recording token. | Low: deploy on Linux; use token in join. |

**Practical recommendation:** Use **Zoom Meeting SDK + recording token**. No RTMS, no third-party services — only Zoom's Meeting SDK (join + raw audio + virtual mic) and Zoom's REST API (recording token). Implement the token fetch in your dashboard/backend and pass it when starting Koov; see [PRODUCTION_HEADLESS.md](PRODUCTION_HEADLESS.md).
