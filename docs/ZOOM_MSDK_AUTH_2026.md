# Zoom Meeting SDK auth (March 2026) and Mechus

From **March 2, 2026**, Zoom requires stricter authorization for Meeting SDK (MSDK) apps that join meetings **outside** the app owner’s account. For continuous or automated recording, Zoom recommends **RTMS** instead.

---

## How this affects Mechus

| Scenario | Today (JWT) | From March 2026 |
|----------|-------------|------------------|
| **Meeting host is in the same Zoom account as your app** (e.g. your company’s Zoom account) | JWT only. Mechus joins with `ZOOM_APP_CLIENT_ID` / `ZOOM_APP_CLIENT_SECRET` (Meeting SDK app). Host allows recording once. | Same: **JWT only** is enough. No OBF required. |
| **Meeting host is in a different Zoom account** (e.g. customer’s Zoom, external user) | JWT may still work in some cases. | **OBF token** required. An **authorized user** (OAuth) must be **in the meeting** for the bot to join. Session ends when that user leaves. |
| **Continuous / automated recording, any account** | Meeting SDK with host allow recording. | Zoom recommends **RTMS**: no “user in meeting” requirement, stream continues even if host leaves. |

**Source:** [Zoom – FAQ: Updates to Meeting SDK authorization](https://developers.zoom.us/docs/meeting-sdk/obf-faq/)

---

## What to use for Mechus

- **Meetings inside your org (same Zoom account as the app)**  
  Keep using **Meeting SDK** (paste link → Mechus joins with JWT). Host allows recording once. No change needed for March 2026.

- **Meetings from external/customer accounts, or you want “no user in meeting”**  
  Use **RTMS**: user has your Zoom App, meeting starts, Zoom streams audio to your server. No OBF, no requirement for a user to stay in the meeting.  
  See [RTMS_SETUP.md](RTMS_SETUP.md). You must get **RTMS enabled** for your Zoom account (request via Zoom).

- **Meeting SDK with external accounts after March 2026**  
  You would need to implement **OBF token** flow: a user authorizes your app via OAuth and is **in the meeting**; your backend gets an OBF token and passes it when starting the join. When that user leaves, the session ends. For “bot joins with no user in meeting,” use RTMS instead.

---

## Summary

- **Same-account meetings:** Meeting SDK + JWT is fine; no OBF.
- **External accounts / continuous recording:** Prefer **RTMS** (request enablement from Zoom).
- **OBF:** Only if you must use Meeting SDK for external accounts and are OK with “authorized user must be in the meeting.”
