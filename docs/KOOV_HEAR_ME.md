# How to Get Koov to Hear You (Zoom SDK)

## Hybrid: RTMS (hear + transcribe) + Bot (speak)

If you want **both**: RTMS for **live diarization and transcription** (no recording prompt) and a **bot in the meeting that actually speaks** (TTS), use the **RTMS + Meeting SDK hybrid**:

| Role | How |
|------|-----|
| **Hear + transcribe** | **RTMS** streams real-time audio (and optional transcript) to Koov. No host “allow recording” prompt; Zoom sends audio when the user joins with your Zoom App. |
| **Speak in the meeting** | A **muted** Meeting SDK **bot** joins the same meeting. Koov unmutes the bot only when it plays a response (wake word / “Koov” / question), then mutes again. |

**What you need:**

- **Two Zoom apps:** (1) **General App** with RTMS (for audio in), (2) **Meeting SDK** app (for the bot that speaks).
- **RTMS enabled** for your Zoom account (request from Zoom if needed).
- **Run:** `python scripts/rtms_koov.py` (or the RTMS Docker stack). Dashboard calls `POST /api/set-meeting-for-rtms` with the meeting link; user joins that meeting **with the Zoom App**; RTMS webhook fires → Koov connects to RTMS for audio and **also** starts the Meeting SDK bot for TTS.
- **Platform:** Bot (TTS) needs **Linux** (Meeting SDK). On macOS you can run RTMS-only (Koov hears and transcribes; responses are logged, no TTS in meeting).

Full steps: **[RTMS_SETUP.md](RTMS_SETUP.md)** — especially section **“4. RTMS + Meeting SDK Hybrid (TTS in meeting)”**.

---

## No voice input? (Meeting SDK only, no RTMS)

If the bot joins but **never receives your voice** (no transcription, no wake word), the cause is almost always **recording permission**: Zoom only sends raw meeting audio after the host allows recording. See **[VOICE_INPUT_ISSUE.md](VOICE_INPUT_ISSUE.md)** for the exact flow and two fixes: (1) host clicks Allow when Zoom prompts, or (2) use a **recording token** so no prompt is needed (script: `scripts/fetch_zoom_recording_token.py`).

---

## Zoom SDK only (listen + speak, single app)

When Koov joins via the **Zoom SDK** only (no RTMS), it gets two things from Zoom:

| Purpose | Zoom SDK feature | What Koov does |
|--------|-------------------|----------------|
| **Listen** to the conversation | **Raw recording** (one-way per participant, mixed fallback) | Prefers `onOneWayAudioRawDataReceived(data, node_id)` so each stream can be mapped to a Zoom participant name. Falls back to `onMixedAudioRawDataReceived` if the binding does not expose one-way callbacks. |
| **Speak** into the meeting | **Virtual mic** (external audio source) | Registers `setExternalAudioSource`; Zoom gives a sender. Koov sends TTS PCM through it so participants hear Koov. |

So Koov **listens** via Zoom’s raw meeting audio stream and **speaks** via Zoom’s virtual mic. Both are set up automatically when Koov joins with the Zoom SDK. If Koov never receives any raw audio frames, it cannot hear you. Use the checklist below.

**Don't want RTMS?** For first-party Zoom only (no third party): Zoom SDK + recording token — see **[AUDIO_WITHOUT_RTMS.md](AUDIO_WITHOUT_RTMS.md)**.

---

## Docker: bot can’t hear me

If you run Koov in **Docker** (Zoom SDK stack) and the bot never hears you:

1. **Confirm you’re using the Zoom SDK stack**  
   You must start with:  
   `./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build`  
   (not the default compose alone). The default image has no audio device in the container.

2. **Host must allow recording**  
   When Koov joins, Zoom may show the **host** a prompt: *“Allow recording?”* or *“Participant requested to record.”*  
   The **host must click Allow**. If they don’t, Zoom never sends raw audio and the bot will not hear.  
   **Host account:** Basic Zoom accounts cannot grant local recording to SDK participants (SDKERR_NO_PERMISSION forever); the host must have recording enabled on their account (e.g. Pro or setting that allows participant recording).  
   Koov requests this when it joins and re-sends the request every 10 seconds until the host allows. In Docker logs you'll see `[Zoom] HOST ACTION: In Zoom, click Allow when prompted...` As the host, watch the Zoom window for a popup or banner and allow the participant "Koov" to record.

3. **Check status API**  
   After Koov has joined, call:  
   `GET http://localhost:5001/api/status?meeting_id=YOUR_MEETING_ID`  
   - **`audio_status`**: `"receiving"` = bot is getting audio; `"no_audio_yet"` = no frames yet (allow recording / speak); `"silent"` = got audio before but none recently.  
   - **`zoom_frames_received`**: If 0, Zoom has not sent any audio (host allow + someone speaking).

4. **Check Docker logs**  
   `./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml logs -f koov`  
   - **“Zoom audio: first frame received; Koov can hear the meeting”** → audio is working.  
   - **“No Zoom audio frames after 30s”** → Zoom isn’t sending audio; re-check host allowed recording, app type (Meeting SDK), and that someone is speaking.  
   - **“CanStartRawRecording returned … (host must allow recording)”** → host has not allowed yet; have the host click Allow when Zoom prompts.

5. **Someone must be speaking**  
   Zoom may only deliver raw audio when there is speech. Have someone talk for 10–30 seconds after the host allows recording.

6. **Zoom app must be Meeting SDK**  
   In [Zoom Marketplace](https://marketplace.zoom.us/) your app must be a **Meeting SDK** app (not only General/OAuth). Use that app’s Client ID and Client Secret in `.env`. See section 1 below.

If you did all of the above and logs still show **subscribe_result=0 setExternal_result=0** but **no frames**, raw audio callbacks can be unreliable in headless Docker (known Zoom SDK / Linux issue). Expose ALSA so callbacks can fire: `--device /dev/snd` and `--group-add audio` (see [VOICE_INPUT_ISSUE.md](VOICE_INPUT_ISSUE.md) §6, §10). Otherwise use a [local recording token](PRODUCTION_HEADLESS.md) or run the SDK on a Linux host with real ALSA.

**“It’s not working” (any flow):** See **[NOT_WORKING.md](NOT_WORKING.md)** to identify which flow you’re using (RTMS vs Zoom SDK) and what to check.

---

## Test audio injection (Koov speaking into the meeting)

To verify that Koov can **speak** into the meeting (TTS via Zoom virtual mic), call the test-voice API **after** the bot has joined and the session is active:

```bash
curl -X POST http://YOUR_SERVER:5001/api/test-voice \
  -H "Content-Type: application/json" \
  -d '{"meeting_id":"YOUR_MEETING_ID","text":"Hello, this is Koov. Voice test."}'
```

- **meeting_id**: Use the same `meeting_id` returned when you called `POST /api/join-meeting` (often the Zoom meeting number as string).
- **text** (optional): Phrase to speak; default is *"Hello, this is a voice test from Koov."*

Participants in the meeting should hear the phrase. If they don't, check that the Zoom SDK virtual mic is ready (see status: `virtual_mic_ready`) and that TTS is configured (e.g. ElevenLabs `KOOV_ELEVEN_API_KEY` in `.env`).

---

## Zoom SDK still not working? Run on real Linux

**If you're running Docker on a Mac:** The Zoom SDK image is Linux (amd64). On a Mac, Docker runs it in **emulation**. In that environment, Zoom's raw audio callbacks often **never fire** — so Koov never receives any frames and cannot hear or use the virtual mic properly. This is a known limitation, not a config bug.

**Fix:** Run the **same** Koov Zoom image on a **native Linux** machine (e.g. AWS EC2, Google Cloud VM, Azure VM). Build/run there with `docker compose -f docker-compose.yml -f docker-compose.zoom.yml up -d` and point your dashboard at that server. Join flow is unchanged; Koov will run on Linux and receive Zoom audio. For more reliable raw-audio callbacks (avoid dummy ALSA in containers), run the **Zoom SDK process outside Docker** and use Docker only for the API/backend; see [VOICE_INPUT_ISSUE.md](VOICE_INPUT_ISSUE.md) §6 and [ZOOM_BOT_ON_UBUNTU_SERVER.md](ZOOM_BOT_ON_UBUNTU_SERVER.md).

**Check status:** `GET /api/status?meeting_id=YOUR_MEETING_ID` — **zoom_frames_received** = 0 means Zoom isn't sending audio (host allow, or run on Linux). **virtual_mic_ready** = false means the virtual mic isn't ready yet (if it never becomes true, run on Linux).

**Alternative:** Use a [local recording token](PRODUCTION_HEADLESS.md) so the host never sees "Allow recording?".

---

## 1. Zoom app type and credentials

**Where:** [Zoom Marketplace](https://marketplace.zoom.us/) → Your app → **App Credentials**

- Koov needs a **Meeting SDK** app so it can join meetings and use raw audio. A **General app** (OAuth, Event Subscriptions, webhooks only) will **not** work for Koov’s in-meeting join and audio.
- If your app is “General app” or “User-managed app” with only Event Subscription / Token features, create a **new app** and choose **Meeting SDK** (or add Meeting SDK to your app if your account allows it). Use that app’s Client ID and Client Secret in Koov.
- In Koov’s **`.env`** (repo root), set:
  ```bash
  ZOOM_APP_CLIENT_ID=your_sdk_client_id
  ZOOM_APP_CLIENT_SECRET=your_sdk_client_secret
  ```
- Restart Koov (or rebuild the Docker image) after changing `.env`.

---

## 2. Host must allow recording (most common)

**Where:** In the Zoom meeting, as the **host** (or co-host)

When Koov joins and calls **Start Raw Recording**, Zoom may show a prompt to the host:

- **“Allow recording?”** or **“Participant requested to record”**
- The **host must click Allow / Yes** so raw recording (and thus raw audio) can start.

If the host dismisses or blocks it, Koov will not receive any audio.

**What to do:** Start the meeting as host (or have a co-host), start Koov from the dashboard, and when Zoom asks to allow recording, **allow it**.

### The prompt never appears (SDKERR_NO_PERMISSION)

If logs show `CanStartRawRecording returned SDKError.SDKERR_NO_PERMISSION` and the host never sees a prompt:

1. **Enable local recording on the host's account** — Go to [zoom.us](https://zoom.us) → **Settings** → **Recording**. Turn on **Local recording** (or Record to computer files). If this is off, Zoom will not prompt and the bot cannot get audio.

2. **Account setting for participant recording** — Under Settings → Recording, enable options like "Allow hosts to give participants permission to record locally" if available.

3. **Confirm you are the host** — The prompt appears **only to the meeting host** (crown icon). If you joined as a guest, Zoom may consider someone else the host.

4. **Host must be in the meeting first** — Start the meeting, join as host, then trigger Koov. The host must already be in the meeting when Koov requests recording.

If you still never see a prompt, run Koov **natively on your Mac** with BlackHole (see README) instead of Docker—that path doesn't need the Zoom prompt.

### I clicked Allow but still get “No Zoom audio frames”

1. **Check the new warning line** in Docker logs. It now shows `subscribe_result=X setExternal_result=Y`. For Zoom SDK, **0 = success**. If either is non-zero, the SDK call failed (app permissions or meeting type).
2. **Someone must be speaking.** Zoom may only deliver raw audio when there is activity; have someone talk in the meeting for at least 10–15 seconds after allowing.
3. **Zoom app permissions:** [Zoom Marketplace](https://marketplace.zoom.us/) → your **Meeting SDK** app → **Permissions**. Ensure the app has **raw data** / **recording** (or equivalent) so it can use Start Raw Recording and receive audio.
4. **Allow before Koov joins:** Start the meeting as host, wait for the meeting to be in progress, then start Koov. When the prompt appears, click Allow immediately. Some setups work better when the host is already in the meeting before Koov joins.

### Subscribe and setExternal both SUCCESS but still 0 frames

If the log shows `subscribe_result=SDKERR_SUCCESS setExternal_result=SDKERR_SUCCESS` but “No Zoom audio frames after 30s”:

1. **Someone must be speaking.** Zoom may only deliver raw audio when there is active speech. Have someone talk **continuously** for 30+ seconds (e.g. read a paragraph out loud).
2. **Meeting type:** Use a **regular Zoom meeting**, not a webinar. Some meeting types may not deliver raw audio.
3. **Linux/Docker:** Raw audio callbacks can be platform-sensitive. If possible, try running the API on the host (not in Docker) with the Zoom SDK to see if audio appears.
4. **Host allow recording:** Confirm the host clicked Allow when Zoom asked; if in doubt, leave and rejoin the meeting and allow again.

---

## 3. Koov logs – is Zoom sending audio?

**Where:** Terminal running Koov (or Docker logs)

```bash
./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml logs -f koov
```

**Look for (in order):**

| Log line | Meaning |
|----------|--------|
| `Zoom audio subscribe result: 0` (or success) | Raw audio subscription succeeded. |
| `Zoom audio: using one-way raw audio...` | Zoom-native per-speaker attribution is active. |
| `Zoom audio: using mixed audio fallback...` | SDK binding does not expose one-way callbacks, so speaker attribution falls back to unlabeled mixed audio. |
| `Zoom audio: first frame received; Koov can hear the meeting` | **Zoom is sending audio** → Koov can hear. |
| `Zoom audio: N frames received in last 60s` | Ongoing audio is being received. |
| `No Zoom audio frames received after 30s...` | **No audio from Zoom** → see steps 1–2 and 4. |

If you **never** see “first frame received”, Zoom is not delivering raw audio to Koov. **Most often:** the host did not click **Allow** when Zoom asked to allow recording. Have the host allow it, then have Koov rejoin or start a new meeting. Also check app type and permissions (steps 1 and 4).

**Wake word:** If logs show `Ignoring word missing in vocabulary: 'koov'`, the default Vosk model does not include "koov". Use **"computer"** (e.g. "Computer, can you hear me?") until you add a custom vocabulary.

---

## 4. Zoom app permissions (raw data)

**Where:** [Zoom Marketplace](https://marketplace.zoom.us/) → Your **Meeting SDK** app → **Permissions** or **Scopes**

- Ensure the app has permission for **raw data** / **recording** (or similar) so the Meeting SDK can use **Start Raw Recording** and receive audio.
- If your app type doesn’t support “raw recording” or “raw data”, create or use a **Meeting SDK** app that does, and use its Client ID/Secret in `.env`.

Exact names depend on Zoom’s UI; look for anything related to **recording** or **raw data** for the Meeting SDK.

---

## 5. Say the wake phrase

**Where:** In the same Zoom meeting, after Koov has joined and you’ve allowed recording

- Default wake phrase in `.env`: **`KOOV_WAKE_PHRASE=computer`** . With the default Vosk model, "koov" is not in the vocabulary; use **"computer"**.
- Say clearly: **“Computer”** or **“Computer, hello”** (or **“Koov”** if you set `KOOV_WAKE_PHRASE=koov`).
- Koov only responds after it **receives audio** (see step 3) and detects the wake phrase.

---

## Quick checklist

1. [ ] `.env` has `ZOOM_APP_CLIENT_ID` and `ZOOM_APP_CLIENT_SECRET` from a **Meeting SDK** app.
2. [ ] Koov restarted (or Docker image rebuilt) after changing `.env`.
3. [ ] **Host** allowed recording when Zoom prompted after Koov joined.
4. [ ] Logs show **“Zoom audio: first frame received”** (if not, no audio → re-check 1–3 and app permissions).
5. [ ] You said the wake phrase (**“computer”** or **“Koov”**) clearly in the meeting.

For more on Zoom SDK setup (including Mac workaround without SDK), see **ZOOM_SDK_SETUP.md**.

**Production / headless:** To avoid host prompts and mute-on-entry, see **PRODUCTION_HEADLESS.md** (recording token, meeting creation settings, Recall.ai).
