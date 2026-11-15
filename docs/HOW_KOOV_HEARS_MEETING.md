# How the API Gets Input and Hears What’s Going On in the Meeting

This doc summarizes how Koov (and similar apps) can receive meeting audio so they can “hear” what’s said, and what Zoom actually provides.

---

## 1. Zoom Meeting SDK (what Koov uses today)

**This is the only way to get real-time raw audio (PCM) from a meeting when your app joins programmatically.**

### Flow

1. **Join** – Your app joins the meeting with the Meeting SDK (e.g. `JoinParam`, meeting number, password, `SDK_UT_WITHOUT_LOGIN`). JoinParam can request raw audio: `eAudioRawdataSamplingRate = 32K`, etc.

2. **Recording privilege** – After join, the **host** must allow recording when Zoom prompts (“Allow recording?” / “Participant requested to record”). Without this, raw audio is not delivered.

3. **Start Raw Recording** – Your app calls `GetMeetingRecordingController().StartRawRecording()`. This enables the raw-data pipeline; it does **not** start Zoom’s built-in cloud/local recording.

4. **Get Audio Helper** – `GetAudioRawdataHelper()` returns `IZoomSDKAudioRawDataHelper`.

5. **Subscribe to raw audio** – `audio_helper.subscribe(delegate, withInterpreters)` where `delegate` implements:
   - **`onMixedAudioRawDataReceived(data)`** – mixed audio from all participants (best for “hear everyone,” including wake word).
   - **`onOneWayAudioRawDataReceived(data, user_id)`** – per-participant audio.

6. **Callbacks** – Zoom calls your delegate with `AudioRawData` (PCM buffers). Your app pushes these into a queue; the agent reads frames, runs wake-word detection, STT, etc.

7. **Virtual mic (optional)** – `audio_helper.setExternalAudioSource(virtual_mic_callbacks)` so your app can **send** audio into the meeting (e.g. TTS). Koov uses this so participants hear Koov speak.

### Requirements

- **Meeting SDK app** (not General/OAuth-only). Programmatic join enabled (e.g. “Request Anonymous Join Exception”).
- **Host allows recording** when Zoom prompts after your bot joins.
- **Raw data / recording** permission for the Meeting SDK app (Zoom Marketplace → app → Permissions).
- **Linux** for the native Meeting SDK (Koov uses the Linux SDK in Docker; on Mac/Windows you use `join_backend=browser` or `external` with virtual audio).

### Known issues (Linux / headless / Docker)

- **Audio callbacks may not fire** on headless Linux without physical audio hardware. Community reports: ALSA errors, subscribe returns success but no `onMixedAudioRawDataReceived` / `onOneWayAudioRawDataReceived` callbacks (e.g. on EC2, Docker). Installing PulseAudio sometimes helps; otherwise running on a host with real audio or a different platform may be needed.
- Zoom’s Linux raw recording sample repo has been removed from GitHub; the flow above is the intended sequence (init → join → StartRawRecording → subscribe → consume PCM in callbacks).

---

## 2. Alternatives (no raw PCM from Meeting SDK)

### Zoom REST API

- **No live meeting audio stream.** The REST API does not provide a real-time audio stream of an ongoing meeting.
- **Post-meeting:** Cloud recording + Recording APIs (e.g. list recordings, download) plus your own transcription (e.g. Whisper, AssemblyAI). Not suitable for “hear and respond in real time.”

### Real-Time Media Streams (RTMS) / WebSockets

- Zoom has **Real-Time Media Streams** with WebSockets for **live transcripts** (and possibly audio) in some products. This is a different path from the Meeting SDK and may require different app types / entitlements / Zoom plan.
- If you only need **live transcript** (not raw PCM for wake word + STT), RTMS could be an alternative to explore in Zoom’s current docs.

### Browser / “external” join (what Koov supports today)

- **You** join the meeting in a browser or Zoom app. You set the OS/browser audio output to a **virtual device** (e.g. BlackHole). Koov runs on the host and uses that virtual device as its **input**. So the “API” (Koov) gets input by reading from the same audio device that is receiving the meeting audio. No Meeting SDK in the loop; works on Mac/Windows with virtual audio.

---

## 3. How Koov’s API “gets input” today

| Join mode              | How the API “hears” the meeting |
|------------------------|----------------------------------|
| **Zoom SDK** (Docker)  | Meeting SDK → Start Raw Recording → subscribe → `onMixedAudioRawDataReceived` / `onOneWayAudioRawDataReceived` → `ZoomAudioInput.push_frame()` → agent reads queue, wake word, STT, LLM, TTS → `setExternalAudioSource` for playback. |
| **Browser**            | Koov opens a browser to the meeting link; you set Zoom/Meet mic/speaker to a virtual device; Koov uses that device as input (and optionally output). |
| **External**           | You join the meeting yourself; Koov uses `KOOV_INPUT_DEVICE` (e.g. BlackHole) as input. Meeting audio is routed to that device so Koov “hears” it. |

So for **Docker + Zoom SDK**, the API gets input **only** via the Meeting SDK raw audio callbacks. If those callbacks never fire (e.g. headless Linux, host didn’t allow recording, wrong app type), Koov will not hear the meeting.

---

## 4. Summary

- **To have the API “hear” the meeting in real time with the Meeting SDK:**  
  Use the **Meeting SDK** on Linux: join → host allows recording → Start Raw Recording → subscribe with `IZoomSDKAudioRawDataDelegate` → implement `onMixedAudioRawDataReceived` (and optionally `onOneWayAudioRawDataReceived`) → feed PCM into your pipeline (e.g. Koov’s `ZoomAudioInput`).

- **If raw audio callbacks still don’t fire** after correct app type, permissions, and host allowing recording: try a host with real audio (or PulseAudio), or use **browser** / **external** join with virtual audio so the API gets input from the OS audio device instead of the SDK.

- **Zoom REST API** does not provide live meeting audio; **RTMS/WebSockets** may provide live transcript (and possibly audio) and is a separate option to check in Zoom’s current documentation.

---

## 5. Production: replicable for multiple meetings

For a **production-level product** that can **replicate across many meetings** (no per-meeting manual setup), you have three realistic paths.

### Option A: Meeting-bot / join API (e.g. Recall.ai)

- **What:** A third-party API that joins Zoom (and other platforms) as a bot and gives you **real-time audio + transcript** (and optionally video). You send a meeting URL; they join and stream audio/transcript to you.
- **Pros:** No Zoom SDK or audio driver work; multi-meeting by design; one integration for Zoom, Meet, Teams, etc.; SLA and scaling handled by the provider.
- **Cons:** Paid; dependency on vendor; you may need to adapt Koov to consume their stream format (e.g. WebSocket or webhooks) instead of the current Zoom SDK → `ZoomAudioInput` path.
- **Best for:** Production at scale when you want to **own the product logic** (wake word, STT, LLM, TTS) but **not** the “join meeting and get audio” infra.

### Option B: Zoom RTMS (Realtime Media Streams)

- **What:** Zoom’s WebSocket-based platform for **real-time audio, video, and transcript** from meetings. The [RTMS Meeting Assistant Starter Kit](https://github.com/zoom/rtms-meeting-assistant-starter-kit) shows **multiple concurrent meetings** (per-meeting `streamId`, per-participant audio, real-time transcript).
- **Pros:** Zoom-native; real-time streams; designed for AI/assistant use; multi-meeting in one app (per-stream directories, WebSocket per stream).
- **Cons:** Tied to **Zoom Apps** (users install your app; RTMS runs when they are in a meeting). So it’s “meeting started by a user who has your app,” not “bot joins any arbitrary meeting link.” App type, entitlements, and pricing are in Zoom’s docs/sales.
- **Best for:** Production when your **users are in Zoom** and you’re building a **Zoom App** (e.g. in-meeting assistant). Not for “join any meeting URL without the host having your app.”

### Option C: Meeting SDK at scale (your own infra)

- **What:** Run the **Zoom Meeting SDK** yourself (e.g. one process or container per meeting, or a pool of workers). Use the same flow as today: join → Start Raw Recording → subscribe → consume PCM in callbacks.
- **Pros:** Full control; no per-seat vendor cost for join/audio; you already have the Koov pipeline (wake word, STT, LLM, TTS).
- **Cons:** Raw audio callbacks are **unreliable on headless Linux/Docker** (no frames despite subscribe success). You need either:
  - **Align with Zoom’s official headless path:** e.g. [Zoom Meeting SDK Headless Linux Sample](https://github.com/zoom/meetingsdk-headless-linux-sample) (Docker, C++), or a Python stack on the **same** base image / audio setup (e.g. Ubuntu + Zoom SDK + optional PulseAudio) and validate that callbacks fire.
  - **Run on Linux with real or virtual audio:** e.g. VMs/containers with PulseAudio or a virtual sound device so the SDK has something to bind to; avoid “pure” headless Docker-on-Mac for the join process.
- **Best for:** Production when you want to **own the full stack** and are willing to run Linux join-workers with a proven Zoom headless/audio setup (or migrate to a join API for the “get audio” part).

---

## 6. Getting raw audio callbacks to fire in headless Docker

Zoom’s **official** headless Linux sample uses **PulseAudio + ALSA** in Docker; with **ALSA dummy only**, raw audio callbacks often never fire even when subscribe/setExternal succeed. Koov’s Zoom image is aligned with that approach.

### What Koov does

1. **Dockerfile.zoom** installs the same audio stack as Zoom’s headless sample:
   - **ALSA:** `libasound2`, `libasound2-plugins`, `alsa-utils`
   - **PulseAudio:** `pulseaudio`, `pulseaudio-utils`
   - **D-Bus:** `dbus` (needed for user-mode PulseAudio)

2. **Entrypoint** (`docker-entrypoint-zoom.sh`) runs **before** gunicorn:
   - Starts a **D-Bus session** and **PulseAudio** in user mode (`pulseaudio -D --exit-idle-time=-1`).
   - Loads a **null sink** (`module-null-sink`) so the Zoom SDK has a virtual output to attach to.
   - If D-Bus or PulseAudio fail (e.g. in a minimal host), the script still runs gunicorn so the container doesn’t break.

3. **ALSA** is still set to a **dummy** default so the SDK can open a device even when PulseAudio isn’t running. Once PulseAudio is confirmed working, you can point ALSA default at Pulse (e.g. `pcm.!default { type pulse }`) so the SDK uses Pulse’s virtual device.

### What you can do

1. **Rebuild and test**  
   Rebuild the Zoom image and join a meeting (host allows recording, someone speaks for 30+ seconds). Check logs for `Zoom audio: first frame received` and `Zoom audio: N frames in last 5s`.

2. **If callbacks still don’t fire**  
   - **Run on Linux with real audio:** Use a Linux host (or VM) with a real sound device or a virtual loopback (e.g. `snd-aloop`) and run the same stack there (with or without Docker).  
   - **PulseAudio in system mode:** Run the container as **root** (remove `USER appuser` in the Dockerfile for the run), create `/var/run/pulse` and a pulse user/group if needed, and start PulseAudio with `pulseaudio --system -D --disallow-exit --exit-idle-time=-1`. Then run gunicorn as appuser (e.g. `runuser -u appuser -- gunicorn ...`) from the entrypoint.  
   - **Use Zoom’s official sample:** Build and run [Zoom Meeting SDK Headless Linux Sample](https://github.com/zoom/meetingsdk-headless-linux-sample) (C++, same PulseAudio + ALSA setup) to confirm raw audio in your environment, then mirror that setup for Koov.

3. **Reference**  
   Zoom’s headless sample Dockerfile: Ubuntu 24.04, `libasound2t64`, `libasound2-plugins`, `alsa`, `alsa-utils`, `alsa-oss`, `pulseaudio`, `pulseaudio-utils`. Koov uses the same idea (PulseAudio + ALSA + entrypoint) so raw audio callbacks have a chance to fire in headless Docker.

### Recommendation (production, multi-meeting)

| Goal | Best option |
|------|-------------|
| Ship fast, many meetings, minimal infra | **Option A** – Use a meeting-bot/join API (e.g. Recall.ai); adapt Koov to consume their real-time audio/transcript. |
| Zoom App, users start meetings with your app | **Option B** – Zoom RTMS; multi-meeting and real-time by design; integrate with Zoom Apps. |
| Full control, own join + audio | **Option C** – Meeting SDK on Linux with a setup that gets callbacks (e.g. Zoom headless sample approach or PulseAudio/virtual audio); scale workers per meeting. |
