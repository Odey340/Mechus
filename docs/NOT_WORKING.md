# "It's not working" – what to check

Use this page to narrow down which flow you’re using and what to verify.

---

## "Koov API is ready to use" but I can't use it

Something says Koov is ready (e.g. Zoom Marketplace, dashboard, or a health check), but when you try to use it nothing works. Here's what "ready" usually means and what you still have to do.

### If the message is from the Koov API (e.g. `/ready` or dashboard "API ready")

**"Ready" only means the API process is running.** It does **not** mean a meeting is active or that Zoom is connected.

To actually use Koov you must:

1. **Start a meeting from the dashboard** (or have a meeting ID/link).
2. **Trigger Koov to join** that meeting:
   - **Zoom SDK:** Dashboard (or you) calls `POST /api/join-meeting` with `meeting_link` and optional `meeting_id` / `user_id`. Koov (bot) joins the meeting; the **host must allow recording** when Zoom prompts.
   - **RTMS:** Dashboard calls `POST /api/set-meeting-for-rtms` with the same meeting link, then **you join that meeting with your Zoom App** (the one you created in the Marketplace). Zoom sends a webhook and Koov connects.
3. **Use the right app type:** Zoom SDK flow needs a **Meeting SDK** app (Client ID/Secret in `.env`). RTMS flow needs a **General App** with RTMS enabled for your account and webhook configured.

**Quick checks:**

- **Dashboard:** Did you click "Start meeting" / "Join with Koov" (or equivalent) after creating the meeting? Ready only means the backend is up; you must start the session for that meeting.
- **Zoom SDK:** After join, check `GET /api/status?meeting_id=YOUR_MEETING_ID`. If `zoom_frames_received` is 0, the bot isn't getting audio (host allow recording, or run on Linux if you're on Mac Docker). See [§ 3 – Zoom SDK](#3-zoom-sdk-flow-not-working).
- **RTMS:** Are you joining the meeting **with the Zoom App** (not just the normal Zoom client)? Is the webhook URL reachable from the internet (no localhost)? See [§ 2 – RTMS](#2-rtms-flow-not-working).

### If the message is from Zoom Marketplace ("App added" / "Ready to use")

That only means the app is installed on your Zoom account. It does **not** start Koov or join any meeting.

- **For RTMS:** You still need to (1) run Koov (e.g. Docker RTMS stack), (2) have the dashboard call `POST /api/set-meeting-for-rtms` with the meeting link, (3) **join the meeting using that Zoom App** so Zoom sends the RTMS webhook. "Ready to use" in Marketplace does not trigger any of that.
- **For Zoom SDK (bot join):** You need a **Meeting SDK** app (not only a General App). Put its Client ID and Secret in Koov's `.env`, then start Koov and use the dashboard (or API) to call `POST /api/join-meeting` with the meeting link. The bot will join; host must allow recording.

If you're not sure which flow you're using, see [§ 1](#1-which-flow-are-you-using) below.

---

## 1. Which flow are you using?

| You did this | Flow | Go to section |
|--------------|------|----------------|
| `./docker-compose-rtms.sh up` and **paste meeting link in dashboard** → user joins with **Zoom App** | **RTMS** (RTMS = mic, optional SDK bot = voice) | [§ 2 – RTMS](#2-rtms-flow-not-working) |
| `./docker-compose-zoom.sh up` (or Zoom SDK stack) and **POST /api/join-meeting** with a Zoom link | **Zoom SDK** (bot joins, hears via raw recording) | [§ 3 – Zoom SDK](#3-zoom-sdk-flow-not-working) |

---

## 2. RTMS flow not working

**Expected:** User pastes link in dashboard → dashboard calls `POST /api/set-meeting-for-rtms` → user joins that meeting **with your Zoom App** → Zoom sends webhook → Koov connects (RTMS = mic, optional bot = voice).

### Check A: Webhook reachable

Zoom must reach your server. **`localhost` does not work.**

- **Local:** Run `ngrok http 8080`, then in Zoom Marketplace → your **General App** → Event Subscriptions → Webhook URL = `https://YOUR_NGROK_URL/webhook`.
- **Cloud:** Webhook URL = `https://your-domain.com/webhook` and port 8080 is open.

**Test:** `curl -X POST https://YOUR_URL/webhook -H "Content-Type: application/json" -d '{"event":"test"}'` → 200. In **rtms** container logs you should see: `[RTMS] Webhook received: event='test'`.

### Check B: Env in Docker

In `.env` (and passed into the **rtms** container):

- `ZM_RTMS_CLIENT` = Zoom App (General App) client ID  
- `ZM_RTMS_SECRET` = Zoom App client secret  

Without these, RTMS join will fail.

### Check C: Event name

When someone starts a meeting **with your Zoom App**, Zoom sends `meeting.rtms_started` (or `meeting.rtms.started`). In **rtms** logs look for:

- `[RTMS] Webhook received: event='meeting.rtms_started'` → good.  
- `[RTMS] Webhook received: event='...'` then `[RTMS] Ignored (...)` → event name doesn’t match; check Zoom docs for your app type.  
- No webhook log at all → Zoom can’t reach your URL (ngrok / firewall / wrong URL).

### Check D: RTMS enabled

RTMS must be **enabled for your Zoom account** (request via Zoom Developer Forum if needed). Otherwise the webhook may fire but `client.join()` fails.

### Check E: Dashboard stored the meeting

Before the user joins with the Zoom App, the dashboard must call:

`POST /api/set-meeting-for-rtms` with `{"meeting_link":"https://zoom.us/j/MEETING?pwd=...","user_id":"..."}`.

Use a **real** Zoom link (meeting number 9–11 digits). Then the user joins **that same meeting** with the Zoom App.

### Logs (RTMS)

```bash
docker logs koovdemo-rtms-1 -f
```

Look for: `[RTMS] Webhook received`, `[RTMS] Joined stream`, `RTMS audio: first frame received`, or `[RTMS] Join failed`.

---

## 3. Zoom SDK flow not working

**Expected:** You call `POST /api/join-meeting` with a Zoom link → Koov (bot) joins the meeting → host allows recording → bot hears and can speak.

### Check A: Real meeting link

Use a real Zoom link, e.g. `https://zoom.us/j/123456789012?pwd=xxxx`. Meeting number is usually **9–11 digits**. A link like `https://zoom.us/j/123` will return 200 from the API but the join will fail in the container.

### Check B: Host allows recording

When the bot joins, Zoom may show the **host** “Allow recording?”. If the host doesn’t click **Allow**, the bot will **not** receive audio. See [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md).

### Check C: Status API

After join:

```bash
curl "http://localhost:5001/api/status?meeting_id=MEETING_ID"
```

Use the `meeting_id` from the join response. Check:

- `audio_status`: `"receiving"` = getting audio; `"no_audio_yet"` = no frames yet.  
- `zoom_frames_received`: 0 = Zoom hasn’t sent audio (allow recording + someone speaking).

### Check D: Logs (Zoom SDK)

```bash
docker logs koovdemo-koov-1 -f
```

Look for: `Zoom audio: first frame received`, `CanStartRawRecording`, `No Zoom audio frames after 30s`. See [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md).

---

## 4. Quick commands

| Goal | Command |
|------|--------|
| Health (API) | `curl http://localhost:5001/health` |
| RTMS webhook test | `curl -X POST https://YOUR_NGROK_OR_DOMAIN/webhook -H "Content-Type: application/json" -d '{"event":"test"}'` |
| Koov (Zoom SDK) logs | `docker logs koovdemo-koov-1 -f` |
| RTMS logs | `docker logs koovdemo-rtms-1 -f` |
| Status for a meeting | `curl "http://localhost:5001/api/status?meeting_id=ID"` |

If you describe what you ran (which compose, which API call, what you see in logs), we can narrow it down further.
