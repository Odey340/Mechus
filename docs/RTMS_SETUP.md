# Zoom RTMS Setup for Koov

Zoom RTMS (Realtime Media Streams) delivers **real-time audio and transcript** from meetings when users have your Zoom App installed. No bot joins for *input*—Zoom streams the meeting to your server.

## RTMS (live diarization + transcription) + Bot (speaks in meeting)

You can use **both**: RTMS for **live diarization and transcription** (audio in), and a **bot in the meeting that actually speaks** (TTS out).

| Role | How |
|------|-----|
| **Hear + transcribe (live diarization)** | **RTMS** streams real-time audio (and optional transcript) to Koov. No "allow recording" prompt; Zoom sends audio when the user joins with your Zoom App. Koov does STT and diarization on this stream. |
| **Speak in the meeting** | A **muted** Meeting SDK **bot** joins the same meeting. Koov unmutes the bot only when it plays a response (wake word / "Koov" / question), then mutes again. |

This is the **RTMS + Meeting SDK hybrid** (section 4 below). Requirements: **Linux** (for Meeting SDK bot), both a **General App** (RTMS) and a **Meeting SDK** app (TTS), and RTMS enabled for your Zoom account. On macOS you can run RTMS-only (Koov hears and responds, but output is logged only—no TTS in the meeting).

**Want “paste a meeting link, Koov joins on its own in Docker”?** Use the **Zoom Meeting SDK** only: [RUN_KOOV_DOCKER.md](RUN_KOOV_DOCKER.md) (section “Docker with audio (Zoom SDK)”). RTMS is for the flow where the user starts the meeting *with* your Zoom App and you use RTMS for input + optional SDK bot for TTS.

---

## Requirements

- **Zoom App** (General App type, not Meeting SDK) with RTMS entitlements
- **RTMS enabled** for your Zoom account (request from Zoom if needed)
- **Platform:** macOS (darwin-arm64) or Linux (linux-x64) — rtms package supports both

---

## Zoom won’t let me add the app / RTMS not available

**RTMS is not enabled for every Zoom account by default.** If the Marketplace won’t let you add RTMS, or you don’t see RTMS options when creating the app, your account likely doesn’t have RTMS yet. You have to **request RTMS enablement** from Zoom first.

### 1. Request RTMS for your account

Choose one:

- **Contact Zoom (recommended)**  
  Go to **[Zoom Realtime Media Streams](https://zoom.com/en/realtime-media-streams)** and use **“Contact our sales team”** / the form at the bottom. Say you want to build an app using Realtime Media Streams and need RTMS enabled for your Zoom account. Include the email of the Zoom account you use for development.

- **Developer Forum**  
  In the **[Zoom Developer Forum](https://devforum.zoom.us/)**, open a topic in the **Realtime Media Streams** category. Ask Zoom to enable RTMS for your account and include:
  - The Zoom account email
  - Your use case (e.g. in-meeting AI assistant, live transcription)
  - That you’re building a **General App** with RTMS event subscriptions and scopes

Until Zoom enables RTMS for your account, RTMS won’t work (e.g. “Granted Capabilities” stays empty, `meeting.rtms.started` never fires). There’s no way to self-enable it in the Marketplace.

### 2. After RTMS is enabled

Once Zoom confirms RTMS is enabled for your account:

1. In [Zoom Marketplace](https://marketplace.zoom.us/) → **Develop** → **Build App**
2. Choose **Create App** → **General App**
3. Complete app details and add the **RTMS** features / event subscriptions / scopes Zoom shows for your account.

### 3. App is "Draft" or "General app" – is that OK?

- **For RTMS:** You need a **General app** (not Meeting SDK). So a **General app** like koovapi is correct. **Draft** is usually fine for development; add Event Subscriptions (e.g. `meeting.rtms.started`, `meeting.rtms.stopped`), webhook URL, and RTMS scopes in the app config. RTMS still only works after Zoom enables RTMS for your account.
- **For “paste link → Koov joins” (listen + virtual mic):** That flow uses the **Meeting SDK**, not a General app. You need a **Meeting SDK** app (create a second app in Marketplace → Build App → **Meeting SDK**). Use that app’s Client ID and Client Secret in Koov’s `.env` as `ZOOM_APP_CLIENT_ID` and `ZOOM_APP_CLIENT_SECRET`. Your existing General app (koovapi) can stay for RTMS; use the Meeting SDK app for the bot-join flow.

### 4. If you can’t get RTMS yet

Use the **Meeting SDK** path instead: paste meeting link → Koov joins as a bot → host allows recording once. See [RUN_KOOV_HEAR_ME.md](RUN_KOOV_HEAR_ME.md) (Production option 1). No RTMS required.

---

## 1. Create a Zoom App with RTMS

*(RTMS options in the Marketplace appear only after Zoom has enabled RTMS for your account. If you don’t see them, use the “Zoom won’t let me add the app” section above.)*

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) → Develop → Build App
2. Choose **Create App** → **General App**
3. Fill in app details
4. Add **Event Subscriptions** and subscribe to:
   - `meeting.rtms.started`
   - `meeting.rtms.stopped`
5. Set your **Webhook URL** to `https://your-server:8080/webhook` (or your host and port)
6. Add **Scopes** for RTMS (Zoom will list required scopes in the app config)
7. **Request RTMS enablement** for your account: [Zoom Developer Forum](https://devforum.zoom.us/t/request-to-enable-realtime-media-streams-rtms-for-my-zoom-account/141880)

---

## 2. Install and Run

### Option A: Docker (all-in-one)

```bash
# From repo root
./docker-compose-rtms.sh up -d --build
```

This runs:
- **Koov API** on port 5001 (dashboard: join-meeting, set-meeting-for-rtms)
- **RTMS webhook** on port 8080 (Zoom sends meeting.rtms_started here)

**Webhooks from Zoom need a public URL.** When running locally:
1. Run `ngrok http 8080` (install from ngrok.com)
2. Copy the HTTPS URL (e.g. `https://abc123.ngrok-free.app`)
3. In Zoom Marketplace → your App → Event Subscriptions → set Webhook URL to `https://YOUR_NGROK_URL/webhook`

When deployed (cloud), set the webhook to `https://your-domain.com/webhook` and expose port 8080.

### Option B: Native (no Docker)

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-rtms.txt
python scripts/rtms_koov.py
```

Same webhook setup: Zoom needs a reachable URL (ngrok for local).

---

## 3. User Flow

1. User installs your Zoom App
2. User starts a Zoom meeting (as host or participant)
3. Zoom fires `meeting.rtms_started` → your server receives it
4. Koov connects to the RTMS stream and receives real-time audio
5. Koov listens for wake phrase (e.g. "computer"), transcribes, responds

---

## 4. RTMS + Meeting SDK Hybrid (TTS in meeting)

**rtms_koov.py** can use RTMS for audio in and Meeting SDK for audio out: RTMS = no recording prompt, real-time audio in; Meeting SDK bot joins muted and unmutes only when Koov responds. Requires Linux, both Zoom apps (General + Meeting SDK), and meeting number/password. On macOS: RTMS-only (no TTS).

### Dashboard paste flow

When your dashboard lets users **paste a meeting link**:

1. **Run with Docker** (API + RTMS in one stack):
   ```bash
   ./docker-compose-rtms.sh up -d --build
   ```
   Or run API and rtms_koov separately (native); both must share the same directory so the store file matches.

2. **Dashboard** – when user pastes link:
   ```javascript
   const res = await fetch(`${KOOV_API_URL}/api/set-meeting-for-rtms`, {
     method: 'POST',
     headers: { 'Content-Type': 'application/json' },
     body: JSON.stringify({
       meeting_link: "https://zoom.us/j/123456789?pwd=...",
       user_id: "user-123"  // optional
     })
   });
   const data = await res.json().catch(() => ({}));
   if (data?.success) {
     // Meeting stored; user can join with Zoom App
   } else {
     // Show error: data?.error || 'Failed to store meeting'
   }
   ```
   **Avoid** `data.success` without checks—if the request fails or returns non-JSON, `data` can be undefined and cause "Cannot read properties of undefined (reading 'success')". Use `data?.success` and handle failures.

3. **User** joins that meeting with the Zoom App. RTMS webhook fires → rtms_koov uses the stored meeting for the Meeting SDK join (TTS).

4. **TTL:** Stored meeting expires after 10 min (configurable via `KOOV_RTMS_MEETING_TTL`).

---

## 4b. RTMS-only (no TTS)

**RTMS is receive-only.** Koov can hear the meeting and generate responses, but cannot speak back into the meeting through RTMS. Responses are logged.

To have Koov speak in the meeting:
- Use **Meeting SDK** (`join_backend=zoom_sdk`) — Koov joins as a bot and uses virtual mic for TTS
- Or **Recall.ai** — full bidirectional audio
- Or build a **Zoom App UI** that displays Koov’s text responses and optionally plays TTS locally

---

## 5. Environment Variables

| Variable | Description |
|----------|-------------|
| `ZM_RTMS_CLIENT` | Zoom App client ID (General App) |
| `ZM_RTMS_SECRET` | Zoom App client secret |
| `ZM_RTMS_PORT` | Webhook server port (default 8080) |
| `ZM_RTMS_PATH` | Webhook path (default `/webhook`) |
| `ZM_MEETING_NUMBER` | Meeting number for SDK join (fallback if no dashboard paste) |
| `ZM_MEETING_PASSWORD` | Meeting password (hybrid mode) |
| `ZOOM_APP_CLIENT_ID` | Meeting SDK app ID (hybrid mode) |
| `ZOOM_APP_CLIENT_SECRET` | Meeting SDK app secret (hybrid mode) |
| `KOOV_RTMS_MEETING_STORE` | Path to store file (default `.rtms_meetings.json`) |
| `KOOV_RTMS_MEETING_TTL` | Seconds until stored meeting expires (default 600) |

---

## 6. Troubleshooting

**“It’s not working”** – See **[NOT_WORKING.md](NOT_WORKING.md)** and use section 2 (RTMS flow). Check: webhook URL reachable (ngrok), `ZM_RTMS_CLIENT` / `ZM_RTMS_SECRET` in `.env`, user joined with Zoom App, RTMS enabled for account. RTMS container logs: `docker logs koovdemo-rtms-1 -f` (look for `[RTMS] Webhook received`, `[RTMS] Join failed`).

- **"Cannot read properties of undefined (reading 'success')"** – Dashboard code is reading `response.success` or `data.success` when the parsed body is undefined. Causes: wrong `KOOV_API_URL` (returns HTML/404), CORS blocking the request, or network failure. Fix: use `data?.success` and `data?.error`; handle non-OK responses: `if (!res.ok) throw new Error(\`API error: \${res.status}\`);` before `res.json()`.
- **No webhook received:** Zoom must reach your webhook URL from the internet. `localhost:8080` does **not** work. Use ngrok (`ngrok http 8080`) and set the Zoom App webhook to `https://YOUR_NGROK_URL/webhook`. Or deploy to a server with a public URL.
- **Check webhook is reachable:** `curl -X POST https://YOUR_URL/webhook -H "Content-Type: application/json" -d '{"event":"test"}'` — should return 200. Or check ngrok's request log.
- **Docker:** `docker logs koovdemo-rtms-1 -f` — should show "Listening for webhook events at http://0.0.0.0:8080/webhook". If Zoom still doesn't reach it, the webhook URL is wrong or behind a firewall.
- **Join fails:** Check ZM_RTMS_CLIENT and ZM_RTMS_SECRET. Ensure RTMS is enabled for your account.
- **No audio:** Verify the meeting has your Zoom App active. Check logs for "RTMS audio: first frame received".
