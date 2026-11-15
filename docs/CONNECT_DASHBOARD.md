# Connect the Dashboard to Koov

## Built-in dashboard (no setup)

The API server ships its own page. Start the server, then open
`http://127.0.0.1:<port>/dashboard` (`/` redirects there). It lets you:

- **Set Sail**: send Koov into a Zoom / Meet / FaceTime call (`POST /api/join-meeting`)
- **The Voyage**: watch status, the live transcript and Koov's thoughts (`GET /api/status`), and end the call (`POST /api/stop`)
- **Scrolls of Ithaca**: add team notes Koov uses in meetings (`/api/teams/<team_id>/context`)
- **Past Voyages**: pick past meetings for Koov to remember (`memory_meeting_ids` on join, or `POST /api/sessions/<id>/memory` mid-call for in-process sessions)

First-time visitors land on `/welcome`, a short intro where they shape Koov's 3D "navigator" and enter their name and team; the choice colours the dashboard hero. Settings → Replay intro brings it back. Add `?demo` to `/dashboard` for sample data (skips the intro). 3D uses three.js and smooth scroll uses Lenis, both vendored in `koov/static/vendor/` so it works offline; reduced-motion and no-WebGL visitors get a static version.

The pages are static files in `koov/static/` with no build step. The header lamp turns
gold when the API is reachable. If `KOOV_API_KEY` is set, enter it under **Ship's papers**: the page itself is public, but every API call still sends the key.

macOS note: AirPlay Receiver listens on port 5000, so set `KOOV_API_PORT=5050` (or turn off AirPlay Receiver) when running locally.

---

Follow the steps below to connect a separate dashboard app so it can start and control Koov (the bot).

---

## Part 1: Run the Koov API server (the “bot”)

The dashboard talks to Koov through an HTTP API. You must start that server first.

### 1. Start the server

From the **koovdemo** repo:

```bash
cd /path/to/koovdemo
source .venv/bin/activate
set -a; source .env; set +a
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

Or use the helper script:

```bash
cd /path/to/koovdemo
./START_SERVER.sh
```

You should see something like:

```
Starting Koov API server on 127.0.0.1:5000
 * Running on http://127.0.0.1:5000
```

**Leave this terminal open** while you use the dashboard.

### 2. Check that it’s running

In another terminal:

```bash
curl http://127.0.0.1:5000/health
```

Expected:

```json
{"status":"ok","service":"koov-api"}
```

If that works, the “bot” (API server) is running and ready for the dashboard.

---

## Part 2: Point the dashboard at Koov

Your dashboard (Next.js or other) must know **where** Koov is and **what** to call.

### 1. Set the Koov API URL

In your **dashboard** project, set the Koov base URL.

**If using `.env.local` (Next.js):**

```bash
# In your dashboard repo
echo 'KOOV_API_URL=http://127.0.0.1:5000' >> .env.local
```

- Same machine: `http://127.0.0.1:5000`
- Koov on another machine: `http://<Koov-machine-IP>:5000`

Restart your dashboard dev server after changing env.

### 2. Call Koov when the user starts a meeting

When the user clicks “Start” / “Join meeting” (or similar), the dashboard must call Koov’s join endpoint.

**Required request:**

- **URL:** `POST {KOOV_API_URL}/api/join-meeting`
- **Headers:** `Content-Type: application/json`
- **Body:**

```json
{
  "meeting_link": "https://zoom.us/j/... or https://meet.google.com/...",
  "user_id": "unique-user-id-from-your-app"
}
```

**Optional body fields:**

- `meeting_id` – your own meeting id (otherwise Koov generates one)
- `display_name` – name shown in the meeting (default `"Koov"`)
- `zoom_access_token` – **required for org Zoom links** (e.g. `https://osu.zoom.us/j/...`). When the user connects Zoom in your app, send their OAuth access token here so Koov can fetch ZAK/recording tokens and actually enter the meeting. Without it, join fails immediately (Zoom error 8) and the host never sees a waiting-room admit prompt.

**Example (fetch):**

```typescript
const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';

const response = await fetch(`${koovApiUrl}/api/join-meeting`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    meeting_link: meeting.meeting_link,  // Zoom or Google Meet link
    user_id: session.user.id,            // required: your user id
    display_name: 'Koov',
    // Org / vanity Zoom links (osu.zoom.us, company.zoom.us) need the user's OAuth token:
    zoom_access_token: session.zoomAccessToken ?? undefined,
  }),
});

const data = await response.json();
if (!response.ok) {
  throw new Error(data.error || 'Failed to start Koov');
}
// data.meeting_id – use this for status and transcript
```

After this call succeeds, Koov will open the meeting in a browser and start the bot. The dashboard is now “connected” for that meeting.

### 3. Optional: status and transcript

Use the **same** `meeting_id` returned from join (or the one you sent) for:

- **Status:** `GET {KOOV_API_URL}/api/status?meeting_id=<meeting_id>`
- **Transcript:** `GET {KOOV_API_URL}/api/transcript?meeting_id=<meeting_id>`
- **Stop:** `POST {KOOV_API_URL}/api/stop` with body `{ "meeting_id": "<meeting_id>" }`

Poll status every 2–5 seconds to show “Joining” → “Active” and use transcript for live/final transcript on the dashboard.

If join fails, `GET /api/status` returns `status: "error"` with `worker_error` / `no_audio_hint` (e.g. missing Zoom OAuth). Surface those fields in the UI instead of staying on “Joining” forever. The join response may also include `zoom_join_warnings` when OAuth is missing.

---

## Checklist

| Step | Where | What to do |
|------|--------|------------|
| 1 | Koov repo | Start API server: `python3 scripts/start_api_server.py` (or `./START_SERVER.sh`) |
| 2 | Koov | Verify: `curl http://127.0.0.1:5000/health` returns `{"status":"ok",...}` |
| 3 | Dashboard | Set `KOOV_API_URL=http://127.0.0.1:5000` (e.g. in `.env.local`) |
| 4 | Dashboard | On “Start meeting”, call `POST /api/join-meeting` with `meeting_link` and `user_id` |
| 5 | Dashboard | Use returned `meeting_id` for `/api/status` and `/api/transcript` if you use them |

---

## Troubleshooting

**“Connection refused” or “Failed to fetch”**

- Koov API server is not running. Start it (Part 1) and check `curl .../health`.
- Wrong URL: confirm `KOOV_API_URL` in the dashboard (and that you restarted the dev server).
- If the dashboard runs in the browser (e.g. Next.js client component), same-origin or CORS: Koov’s server has CORS enabled for dashboard access; if your dashboard is on another host/port, ensure that origin is allowed.

**“user_id is required”**

- Join request must include `user_id` (string). Add it to the JSON body.

**“meeting_link is required”**

- Join request must include `meeting_link` (full Zoom or Google Meet URL).

**Bot doesn’t join the meeting**

- After calling join-meeting, a browser window opens. You (or the user) must click “Join” in Zoom/Meet and set audio to the correct device (e.g. BlackHole). See main README / SETUP_MACOS for audio.

Once the server is running and the dashboard sends `POST /api/join-meeting` with `meeting_link` and `user_id`, the bot is connected to the dashboard for that meeting.
