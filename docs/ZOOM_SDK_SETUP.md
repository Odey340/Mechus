# Zoom SDK Join Setup (Linux)

Koov can join **Zoom** meetings without a browser using the Zoom Meeting SDK. This gives full audio in/out (listen + TTS) with no Chrome or tabs.

**Platform:** **Linux only.** The Python package we use (`zoom-meeting-sdk`) wraps the Zoom Meeting SDK for Linux. **If you're on a Mac (or Windows):** use the workaround below or `join_backend=browser`.

**Per-participant audio + Zoom display names (instead of “unknown” or pyannote SPEAKER_00):** set `KOOV_ZOOM_STREAM_ATTRIBUTION=1` and see **[ZOOM_SPEAKER_ATTRIBUTION.md](ZOOM_SPEAKER_ATTRIBUTION.md)**.

---

## Mac workaround (no Linux, no browser opened by Koov)

On a Mac you can get **Zoom audio into Koov without Koov opening Chrome** by joining Zoom yourself and routing its audio into Koov.

**1. One-time audio setup**

- Install **BlackHole 2ch** (or another virtual audio device).
- In **Audio MIDI Setup**: create a **Multi-Output Device** that includes your speakers and **BlackHole 2ch** (so you still hear Zoom).
- In your **`.env`** set:
  - `KOOV_INPUT_DEVICE=BlackHole 2ch`
  - `KOOV_OUTPUT_DEVICE=BlackHole 2ch` (so Koov’s TTS can be sent into Zoom if you set Zoom’s mic to BlackHole).

**2. When you start a Zoom meeting**

- **You** join the Zoom meeting (Zoom app or browser — your choice).
- In Zoom’s **audio settings**:
  - **Speaker:** your Multi-Output device (so Zoom plays into BlackHole and your speakers).
  - **Microphone:** BlackHole 2ch (so Koov’s TTS is sent into the meeting when Koov speaks).
- From the dashboard, call the API with **`join_backend=external`** (and the same Zoom link if you like, for reference):
  ```http
  POST /api/join-meeting
  Content-Type: application/json

  { "meeting_link": "https://zoom.us/j/...", "user_id": "user-1", "join_backend": "external" }
  ```
- Koov **does not** open a browser. It starts the agent and listens on **BlackHole 2ch**. Because Zoom is outputting to Multi-Output (which includes BlackHole), Koov hears the meeting and can take notes and speak via TTS into the meeting (Zoom mic = BlackHole).

**Summary:** You join Zoom once; Koov never opens a tab. Route Zoom’s output (and optionally mic) through BlackHole so Koov gets meeting audio and can send TTS. No Linux and no Chrome window from Koov.

---

## 1. Run in Docker (Option A)

Run Koov in a **Linux container** on your Mac. The **default image does not include the Zoom Meeting SDK** (the PyPI package requires Zoom’s native Linux SDK headers). Docker is configured with **`KOOV_JOIN_BACKEND=external`**: you join the Zoom meeting yourself and route audio (e.g. BlackHole) so Koov hears and speaks.

**One-time (you):**

1. In the repo root, create **`.env`** with TTS/LLM vars; for Ollama on your Mac use `KOOV_OLLAMA_HOST=http://host.docker.internal:11434`. Zoom creds are optional when using `external` join.

2. Build and run (from the repo root):
   ```bash
   docker compose up --build
   ```

**User:** Join the Zoom meeting (Zoom app or browser). Use **external** join from the dashboard: send `join_backend=external` and the meeting link so Koov starts the agent and listens on your configured input device (e.g. BlackHole). Route Zoom’s output (and optionally mic) through that device so Koov gets meeting audio and can send TTS.

**Dashboard request (external join):**
```http
POST http://localhost:5000/api/join-meeting
Content-Type: application/json

{ "meeting_link": "https://zoom.us/j/MEETING_NUMBER?pwd=PWD", "join_backend": "external" }
```

**Zoom SDK in Docker (workaround – join without browser on Mac):** You can use **`join_backend=zoom_sdk`** in Docker by building the **Zoom image**, which targets **linux/amd64** so pip installs the pre-built `zoom-meeting-sdk` wheel (no native SDK download needed).

1. From the repo root:
   ```bash
   ./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
   ```
2. In **`.env`** set your **Meeting SDK** app credentials (not a General/OAuth-only app):
   ```bash
   ZOOM_APP_CLIENT_ID=your_client_id
   ZOOM_APP_CLIENT_SECRET=your_client_secret
   ```
3. Paste a Zoom meeting link in the dashboard and start the meeting. Koov will join via the Zoom SDK (no browser).

The image is built for **linux/amd64**. On an Apple Silicon Mac, the container runs under emulation; on Intel Mac or Linux amd64 it runs natively. If you prefer the default image (no Zoom SDK), use `docker-compose up` without the zoom override.

**Note:** First build can take a few minutes (Vosk, Whisper, etc.).

---

## 2. Install Zoom SDK dependencies (native Linux only)

On your **Linux** machine (or in a Linux Docker image):

```bash
pip install -r requirements-zoom.txt
```

This installs:
- `zoom-meeting-sdk` – Python bindings for Zoom Meeting SDK (Linux)
- `PyJWT` – JWT auth for Zoom app

**Optional:** If the Zoom SDK needs system libraries (e.g. GLib), install them:

```bash
# Debian/Ubuntu
sudo apt-get install -y libglib2.0-0 python3-gi

# Fedora/RHEL
sudo dnf install -y glib2 gobject-introspection
```

---

## 3. Create a Zoom Meeting SDK app

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) and sign in.
2. **Develop** → **Build App** → choose **Meeting SDK** (or **General App** with Meeting SDK enabled).
3. Create the app and note:
   - **Client ID** (App credentials)
   - **Client Secret** (App credentials)
4. In the app’s **Features** → **Meeting SDK** (or Embed), enable **Meeting SDK** and download the **Linux SDK** if the Python package doesn’t bundle it (the `zoom-meeting-sdk` PyPI package may include binaries; check its docs).
5. Ensure the app is allowed to join meetings as a participant (no special OAuth for “join only” is needed when using JWT with Meeting SDK).

---

## 4. Configure Koov

In your `.env` (or environment):

```bash
# Zoom Meeting SDK (for join_backend=zoom_sdk on Linux)
ZOOM_APP_CLIENT_ID=your_client_id
ZOOM_APP_CLIENT_SECRET=your_client_secret

# Optional: map each Zoom one-way raw audio stream to the participant display name from the SDK (see below)
KOOV_ZOOM_STREAM_ATTRIBUTION=false
```

No need to set `KOOV_INPUT_DEVICE` / `KOOV_OUTPUT_DEVICE` for Zoom SDK join – audio goes through the SDK.

### Who spoke what (stream ↔ name)

When **`KOOV_ZOOM_STREAM_ATTRIBUTION=true`**, Koov feeds STT from **one-way** raw audio only (the mixed “whole room” stream is ignored for transcription). The Zoom callback’s **`node_id`** is treated as a meeting **user id** and resolved to an in-meeting **display name** via the Meeting SDK participant APIs (`GetMeetingParticipantsController` → `GetUserByUserID` / variants → `GetUserName` / variants), with a short TTL cache. If the roster is not ready yet, labels fall back to `user_<id>` until the name resolves.

Implications:

- **`KOOV_DIARIZATION_ENABLED` / Pyannote** are not used while stream attribution is on (each stream is one speaker).
- **Wake word and VAD** see the same per-participant streams, not a single mix of everyone. That matches “who Zoom sends on each one-way channel,” which may differ from hearing every participant in one summed mix.

**`join_backend=external` (e.g. BlackHole):** There is no Zoom SDK roster or per-user audio stream, so this automatic mapping **does not apply**. Use **`POST /api/meeting/speakers`** with the **`meeting_id`** from the join response to map **diarization-style** keys (`Speaker A`, `SPEAKER_00`, …) to display names, and/or enable **`KOOV_DIARIZATION_ENABLED`** for speaker separation. For **`zoom_sdk`**, the same endpoint (or a file under **`KOOV_NOTES_DIR/speaker_maps/`**) can override labels; the worker reloads **`speaker_maps/<safe_meeting_id>.json`** every few seconds (legacy **`speaker-map-<meeting_id>.json`** in the notes root is still read).

Example manual map:

```http
POST /api/meeting/speakers
Content-Type: application/json

{
  "meeting_id": "<uuid from join-meeting response>",
  "speakers": { "Speaker A": "Alice", "user_16777216": "Bob" }
}
```

---

## 4.1 Who Spoke What (Zoom-native attribution)

When you use:

```bash
KOOV_JOIN_BACKEND=zoom_sdk
```

Koov now prefers **one-way raw audio** from the Zoom Meeting SDK for transcription instead of the mixed room stream.

That means:

- `onOneWayAudioRawDataReceived(data, node_id)` is treated as a **per-participant** audio stream.
- Koov resolves that `node_id` to the in-meeting participant list from the SDK.
- Each transcript chunk is tagged with the participant’s **Zoom display name** before STT output is stored.

So the transcript path is:

1. Zoom one-way raw audio callback
2. `node_id -> display name` lookup from the SDK participant roster
3. PCM tagged with `speaker_id` / `speaker_name`
4. STT chunking per speaker stream
5. Transcript stored as `Alice: ...`, `Bob: ...`

Koov does **not** rely on intro text like “I’m Alex” to attach names in this mode.

**Fallback behavior:** if the current Python Zoom SDK binding does not expose one-way raw audio callbacks, Koov falls back to mixed audio. In that fallback case, Zoom-native speaker attribution is not available.

---

## 5. Join a Zoom meeting via the API

**From the dashboard** (or any client), call:

```http
POST /api/join-meeting
Content-Type: application/json

{
  "meeting_link": "https://zoom.us/j/123456789?pwd=AbCdEf",
  "user_id": "user-123",
  "display_name": "Koov",
  "join_backend": "zoom_sdk"
}
```

- **meeting_link:** Must be a Zoom link (e.g. `https://zoom.us/j/MEETING_NUMBER` or `...?pwd=PASSWORD`).
- **join_backend:** `"zoom_sdk"` – use Zoom SDK instead of browser.

The server will:
1. Parse meeting number and password from the link.
2. Auth with Zoom using your Client ID/Secret (JWT).
3. Join the meeting as “Koov” (or your `display_name`).
4. Start raw recording (the **host may need to allow recording** when prompted).
5. Feed meeting audio into Koov and send Koov’s TTS back into the meeting.

**Default join backend for all requests:**

```bash
KOOV_JOIN_BACKEND=zoom_sdk
```

Then you can omit `join_backend` in the JSON.

---

## 5.1 External Join Limitation (BlackHole / no Zoom SDK)

If you use:

```bash
KOOV_JOIN_BACKEND=external
```

Koov does **not** have access to:

- Zoom’s in-meeting participant roster
- per-user raw audio streams
- automatic `name <-> stream` mapping

So true Zoom-native attribution is **not possible** in `external` mode.

In that mode you have two options:

- keep generic speaker labels
- provide a **manual speaker map**

### Manual speaker map API

You can save a per-meeting manual mapping for speaker labels returned by the backend:

```http
POST /api/meeting/speakers
Content-Type: application/json

{
  "meeting_id": "your-meeting-id",
  "speaker_map": {
    "Speaker A": "Alice",
    "Speaker B": "Bob",
    "zoom:16778240": "Alice"
  }
}
```

Read it back with:

```http
GET /api/meeting/speakers?meeting_id=your-meeting-id
```

Saved mappings are automatically applied to:

- `GET /api/transcript`
- `GET /api/status` live transcript entries

When Zoom-native attribution is active, `GET /api/status?meeting_id=...` and `GET /api/transcript?meeting_id=...` also include:

- `participants` — current `userId -> display name` snapshot from the Zoom SDK
- `speaker_attribution_mode` — `zoom_one_way` or `mixed_fallback`

This is intended for:

- `external` join mode
- diarization fallback labels
- post-hoc transcript cleanup when you know who each label refers to

It is **not** LLM inference and it does **not** depend on spoken self-introductions.

---

## 6. Host allows recording

When Koov joins and requests “raw recording” (to get audio), the **host** may see a Zoom prompt to allow recording. The host must **allow** it for Koov to receive and send audio. After that, Koov can listen and take notes and speak via TTS.

---

## 7. Stopping the session

`POST /api/stop` with the same `meeting_id` (or omit for “stop all”) will leave the Zoom meeting and clean up the SDK. No browser to close.

**SDK shutdown:** You may occasionally see **nanobind leak** warnings or rare **heap** messages when the worker exits after `SIGTERM`. Koov calls `leave()` → unsubscribes raw audio → destroys services → `gc.collect()` → short delay → `CleanUPSDK()`. If leaks persist, upgrade `zoom-meeting-sdk` when a newer wheel is available; treat noisy teardown as a known SDK/binding limitation unless you see actual crashes.

---

## 8. Troubleshooting

| Issue | What to do |
|-------|------------|
| “Zoom SDK is only available on Linux” | Use Linux (or Docker with Linux). On macOS/Windows use `join_backend=browser` or `external`. |
| “Zoom SDK requires ZOOM_APP_CLIENT_ID and ZOOM_APP_CLIENT_SECRET” | Set both in `.env` from your Zoom Meeting SDK app. |
| “Could not parse Zoom meeting number” | Use a link like `https://zoom.us/j/123456789` or `...?pwd=xxx`. |
| No audio / “join timed out” | Ensure the meeting is started and the host allows recording when Zoom prompts. |
| **Koov never appears in the meeting** | See “Koov doesn’t appear in the meeting” below. |
| **Error 63 (MEETING_STATUS_FAILED)** | See “Error 63: Join meeting failed” below. |
| ImportError for zoom_meeting_sdk | Run on Linux and `pip install -r requirements-zoom.txt`. |
| GLib / gi import error | Install system GLib and PyGObject (see step 1). |
| **`sqlcipher` / `hmac check failed` / encrypted DB errors on second join** | Stale or corrupted Zoom SDK local cache (often after a crash or unclean exit). See **“Sqlcipher / local DB cache errors”** below. |

### Sqlcipher / local DB cache errors

If logs mention **`sqlcipher`**, **`hmac check failed`**, or **`pgno=`** when starting a new SDK session, the Zoom Meeting SDK’s **local encrypted database** on disk may be corrupt or from an older SDK version.

**What to do (VM / container host):**

1. **Stop** all Koov worker processes using the Zoom SDK.
2. **Remove** the Zoom SDK app data directory for the user that runs the worker (often the service account or `root` in Docker). Typical locations on Linux:
   - `~/.zoom/` or `~/.config/zoom.us/` under that user’s home directory
   - If unsure, search the container/VM for `zoom` + `sqlite` / `sqlcipher` after reproducing once (path varies by SDK build).
3. **Restart** the worker and join again.

**Docker:** wipe the relevant **volume** or add an entrypoint step that clears the SDK cache directory for the container user if `KOOV_ZOOM_SDK_CLEAR_CACHE=1` (implement in your deploy script; Koov does not delete arbitrary paths by default).

**Never paste production logs** that include meeting passcodes or full join URLs (`pwd=`) into public channels — worker logs use **redacted** links; keep that habit everywhere.

### Koov doesn’t appear in the meeting

If the API returns 200 and logs show **join parameters** (meeting number, display name, password sent yes/no — **password values are not logged**) but **“Koov” never shows up in the Zoom participants list**, Zoom is not putting the SDK client into the meeting. Check:

1. **Waiting room** – If the meeting has a waiting room, the **host must admit “Koov”** (or allow participants to bypass the waiting room for this meeting).
2. **Join before host** – In Zoom: **Meeting settings** → enable **“Allow participants to join before host”** (or start the meeting before Koov joins).
3. **Meeting is started** – Someone (host or co-host) must have started the meeting; Koov cannot join a meeting that hasn’t started unless “join before host” is on.
4. **Meeting SDK app** – In [Zoom Marketplace](https://marketplace.zoom.us/) → your Meeting SDK app → ensure it’s **approved/published** as needed for your account and that the meeting isn’t restricted to certain apps.

In Koov logs, look for **`Zoom meeting_status_changed: ... INMEETING`**. If you never see that line, Zoom never reported “in meeting” to the SDK (waiting room, join-before-host, or network/settings). Once you see INMEETING, the host may still need to **allow recording** when Zoom prompts so Koov gets audio.

### Error 63: Join meeting failed

If logs show **`MEETING_STATUS_FAILED iResult=63`**, Zoom rejected the join. Common causes:

1. **Waiting room** – Host must **admit “Koov”** from the waiting room (or disable waiting room for this meeting).
2. **Join before host** – Enable **“Allow participants to join before host”** in the meeting settings, or have the host **start the meeting** before Koov joins.
3. **Meeting not started** – Someone must start the meeting; the SDK cannot join an unscheduled/not-started meeting unless “join before host” is on.
4. **SDK app** – Ensure your [Meeting SDK app](https://marketplace.zoom.us/) is approved and allowed for the meeting/account.

Fix the above and try joining again.

### Using your own meeting link (same Zoom account as the app)

If you use **your own** meeting link and still get error 63:

1. **Start the meeting first** – In the Zoom app or web, **join and start** the meeting yourself, then trigger Koov to join. The meeting must be “live” (host has joined).
2. **Use the full link with password** – If the meeting has a passcode, use a link that includes it, e.g. `https://zoom.us/j/123456789?pwd=AbCdEf`. If you paste a link **without** `?pwd=...` and the meeting requires a passcode, the join will fail (error 63). In Docker logs you’ll see `password_in_link=no`; fix the link or add the passcode.
3. **Waiting room** – Turn off the waiting room for this meeting, or when “Koov” appears in the waiting room, **admit** it.
4. **Join before host** – In Zoom meeting settings, enable **“Allow participants to join before host”** so Koov can join even if you’re not in the meeting yet.
5. **App setup** – In [Zoom Marketplace](https://marketplace.zoom.us/) → your app → complete **Basic Information** and get **Local Test** to “Ready” so the app can join meetings.

In logs, check **`[Zoom] Parsed link: meeting_number=..., password_in_link=yes/no`** and **`[Zoom] Joining meeting ... (password_sent=yes/no)`** to confirm the link and password are correct.

---

## Summary

- **Linux:** Install `requirements-zoom.txt`, set `ZOOM_APP_CLIENT_ID` and `ZOOM_APP_CLIENT_SECRET`, call `POST /api/join-meeting` with `"join_backend": "zoom_sdk"` and a Zoom link. Koov joins with no browser and gets full audio.
- **macOS/Windows:** Use `join_backend=browser` (Chrome) or `join_backend=external` (you provide audio).
