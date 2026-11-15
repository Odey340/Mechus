# How to run Koov so it can hear the meeting (production)

**Production rule:** Koov **joins the meeting by itself**. You do **not** join the meeting for Koov to run. The user starts a meeting from the dashboard (or your app); Koov joins and gets audio via Zoom SDK or RTMS.

---

## Production option 1: Zoom SDK (paste link → Koov joins)

**Flow:** User pastes a Zoom meeting link in the dashboard → your backend calls the Koov API → **Koov (bot) joins the meeting**. Koov **listens** via Zoom’s raw meeting audio (after the host allows recording) and **speaks** via Zoom’s **virtual mic** (external audio source). No need for you or the user to “join for Koov.”

1. **One-time:** Create a **Meeting SDK** app in [Zoom Marketplace](https://marketplace.zoom.us/). In `.env`:
   ```bash
   ZOOM_APP_CLIENT_ID=your_meeting_sdk_client_id
   ZOOM_APP_CLIENT_SECRET=your_meeting_sdk_client_secret
   ```

2. **Run Koov (Docker, Linux or Mac with Docker):**
   ```bash
   ./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
   ```

3. **From your app/dashboard:** When the user starts a meeting, call:
   ```http
   POST http://localhost:5001/api/join-meeting
   Content-Type: application/json
   {"meeting_link":"https://zoom.us/j/MEETING_ID?pwd=PWD","user_id":"user-123"}
   ```
   Koov joins as “Koov”. **Host** sees “Allow recording?” once → must click **Allow** so Koov gets audio (one-time per meeting).

4. Participants speak in the meeting; Koov hears via Zoom’s raw audio and responds to the wake word (e.g. “Computer, …”). Koov’s replies are played into the meeting through Zoom’s **virtual mic** (set up automatically when the bot joins).

**Scaling:** Run more containers behind a load balancer with sticky sessions. Deploy on **Linux** (e.g. cloud VM) for the most reliable Zoom SDK audio; on Mac, Docker uses emulation and raw audio can be flaky.

**Zoom auth (March 2026):** When the host is in the **same Zoom account** as your Meeting SDK app, JWT is enough. When the host is in an **external account**, Zoom will require an **OBF token** (and an authorized user in the meeting). For continuous or multi-tenant use, Zoom recommends **RTMS**. See [ZOOM_MSDK_AUTH_2026.md](ZOOM_MSDK_AUTH_2026.md). See [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md) if the bot doesn’t hear.

---

## Production option 2: RTMS (Zoom streams audio to your server)

**Flow:** User has **your Zoom App** and starts a meeting → Zoom sends `meeting.rtms_started` to your webhook → Koov connects to the RTMS stream and **receives audio from Zoom**. No bot join for input; optionally a muted bot joins the same meeting for TTS so Koov can speak. Again, **you don’t join** — the user is in the meeting with your app, and Zoom streams audio to you.

1. **One-time:** Create a **General App** with RTMS in Zoom Marketplace; get RTMS enabled for your account. In `.env`:
   ```bash
   ZM_RTMS_CLIENT=your_general_app_client_id
   ZM_RTMS_SECRET=your_general_app_client_secret
   ```
   For TTS in the meeting you also need a Meeting SDK app and `ZOOM_APP_CLIENT_ID` / `ZOOM_APP_CLIENT_SECRET`.

2. **Run Koov + RTMS** (e.g. Docker):
   ```bash
   ./docker-compose-rtms.sh up -d --build
   ```
   Expose the RTMS webhook (port 8080) so Zoom can reach it (e.g. ngrok locally or your production URL).

3. **From your app:** When the user is about to join a meeting, call `POST /api/set-meeting-for-rtms` with the meeting link. The user then joins **that** meeting with your Zoom App. Zoom fires the webhook → Koov connects to RTMS and hears the meeting.

**Scaling:** Same as above; multiple instances + sticky sessions. See [RTMS_SETUP.md](RTMS_SETUP.md).

---

## Summary

| Requirement | Zoom SDK (option 1) | RTMS (option 2) |
|-------------|---------------------|------------------|
| Koov joins by itself | ✅ Bot joins when you call the API | ✅ Zoom streams to Koov when user has your app |
| You never join for Koov | ✅ | ✅ |
| Host “Allow recording?” | Once per meeting (Zoom SDK) | Not for input (RTMS streams to you) |
| Production / scale | ✅ Linux recommended | ✅ |

**You do not need to join the meeting for Koov to run.** In production, Koov either joins as a bot (Zoom SDK) or receives audio via RTMS; both paths are designed for that.

---

## Dev only: Mac + BlackHole (you join the meeting)

For **local dev only** (e.g. to test “Koov hears me” on a Mac without Docker/Zoom SDK): you can join the meeting yourself and route Zoom audio through BlackHole; Koov reads from BlackHole. This is **not** the production path and is not scalable. See **SETUP_MACOS.txt** and [DEV_LOCAL_SETUP.md](DEV_LOCAL_SETUP.md) (Option C) if you need it.
