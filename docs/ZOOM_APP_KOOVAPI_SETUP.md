# Zoom app “koovapi” and the March 2026 requirement

Zoom shows this for your app:

> **Beginning March 2, 2026, apps joining meetings outside their account must be authorized. Meet this requirement by using either OBF or ZAK tokens, or RTMS.** [Learn more](https://developers.zoom.us/docs/meeting-sdk/obf-faq/)

Here’s how that maps to Koov and what to do.

---

## What Zoom means

| Option | What it is | When to use |
|--------|------------|-------------|
| **OBF token** | One-time token tied to an **authorized user who is in the meeting**. Bot joins “with” that user; when the user leaves, the session ends. | Meeting SDK, **external** (other Zoom accounts) meetings, and you’re OK with “a user must be in the meeting.” |
| **ZAK token** | Zoom Authentication Key – join **on behalf of** an authenticated user. | Similar to OBF; join as a specific user. |
| **RTMS** | Realtime Media Streams – Zoom streams meeting audio to your server. User has your Zoom App; no “bot join” in the classic sense. | Any account, no “user in meeting” requirement; good for continuous/automated use. |

---

## For Koov (koovapi)

**1. Meetings in the same Zoom account as koovapi**  
- No change. Keep using **Meeting SDK** with your **Client ID** and **Client Secret** (JWT).  
- Put them in Koov’s `.env`: `ZOOM_APP_CLIENT_ID`, `ZOOM_APP_CLIENT_SECRET`.  
- Host may need to allow recording once; for no prompt, use a [recording token](PRODUCTION_HEADLESS.md).

**2. Meetings in other Zoom accounts (external) after March 2026**  
- You must use one of: **OBF**, **ZAK**, or **RTMS**.  
- **RTMS:** Best if you want a bot-like experience without requiring a user to stay in the meeting. Requires RTMS enabled on your Zoom account. See [RTMS_SETUP.md](RTMS_SETUP.md) and [ZOOM_MSDK_AUTH_2026.md](ZOOM_MSDK_AUTH_2026.md).  
- **OBF / ZAK:** Use if you stay on Meeting SDK and are OK with “an authorized user must be in the meeting.” You’d add a flow: user OAuth → backend gets OBF or ZAK → pass to Koov join. See [ZOOM_MSDK_AUTH_2026.md](ZOOM_MSDK_AUTH_2026.md).

**3. App credentials (Development)**  
- **Client ID** `YOUR_ZOOM_APP_CLIENT_ID` and **Client Secret** go in Koov’s `.env` as `ZOOM_APP_CLIENT_ID` and `ZOOM_APP_CLIENT_SECRET`.  
- For **recording token** (same-account, no host prompt): your backend needs a Zoom **OAuth** app (or same app with OAuth) with scope `meeting:read:local_recording_token`, then call `GET /meetings/{meetingId}/jointoken/local_recording` and pass the token to `POST /api/join-meeting` as `recording_token`. See [PRODUCTION_HEADLESS.md](PRODUCTION_HEADLESS.md).

---

## Quick reference

| Goal | Action |
|------|--------|
| Same-account meetings only | Use current Meeting SDK + JWT in `.env`. Optional: add recording token so host doesn’t see “Allow recording.” |
| External meetings after Mar 2026 | Choose RTMS (recommended) or OBF/ZAK; see [ZOOM_MSDK_AUTH_2026.md](ZOOM_MSDK_AUTH_2026.md). |
| No “Allow recording” prompt | Backend fetches [recording token](PRODUCTION_HEADLESS.md) and sends it in `recording_token` when calling Koov’s join API. |
