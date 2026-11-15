# Development / local setup (not for production)

Use this only for development or one-off testing. **Production must run Mechus in Docker (or cloud containers)** and scale horizontally. See [PRODUCTION.md](PRODUCTION.md) and [RUN_MECHUS_DOCKER.md](RUN_MECHUS_DOCKER.md).

---

## Test locally without hosting (no server on the internet)

Everything runs on your machine. No public URL, no ngrok, no VM.

### On Mac (easiest)

1. **Virtual audio (BlackHole)**  
   Install [BlackHole](https://existential.audio/blackhole/) **16ch** and **2ch**. In Audio MIDI Setup, create a **Multi-Output Device** that includes your headphones + **BlackHole 16ch** so Zoom output goes to both.

2. **Zoom routing**  
   In Zoom → Settings → Audio:
   - **Speaker:** your Multi-Output device (so you hear the meeting and Mechus gets a copy via BlackHole 16ch).
   - **Microphone:** **BlackHole 2ch** (so Mechus’s voice goes into the meeting).

3. **`.env`**  
   Copy `.env.example` to `.env` and set at least:
   - `MECHUS_INPUT_DEVICE=BlackHole 16ch`
   - `MECHUS_OUTPUT_DEVICE=BlackHole 2ch`
   - `MECHUS_VOSK_MODEL_PATH=./models/vosk-model-small-en-us-0.15`
   - TTS (e.g. `MECHUS_TTS_BACKEND=elevenlabs`, `MECHUS_ELEVEN_API_KEY`, `MECHUS_ELEVEN_VOICE_ID`).

4. **Vosk model**  
   `python3 scripts/download_vosk_model.py --out models`

5. **Run the demo**  
   ```bash
   source .venv/bin/activate
   python3 scripts/zoom_demo.py
   ```  
   Paste your Zoom meeting link when prompted, then **join that meeting in the Zoom app**. Mechus will listen via BlackHole 16ch and speak via BlackHole 2ch. Say **“Computer”** (or **“Mechus”** if configured) plus your question.

Full step-by-step (including Multi-Output and troubleshooting): **SETUP_MACOS.txt** in the repo root.

### Using the API server locally (still no hosting)

If you want to drive Mechus via the HTTP API (e.g. for a local dashboard):

1. Do the same BlackHole + Zoom routing and `.env` as above.
2. Start the server: `./START_SERVER.sh` (API at **http://localhost:5000**).
3. When joining, use **`join_backend=external`** and **do not** open a meeting in the API — you join the meeting yourself in Zoom. Then call for example:
   ```bash
   curl -X POST http://localhost:5000/api/join-meeting \
     -H "Content-Type: application/json" \
     -d '{"meeting_link":"https://zoom.us/j/YOUR_MEETING","join_backend":"external"}'
   ```
   Mechus will use `MECHUS_INPUT_DEVICE` / `MECHUS_OUTPUT_DEVICE` (BlackHole) to hear and speak. No bot joins Zoom; you are in the meeting, Mechus is just another app on your Mac reading/writing that audio.

### Bot joins without you (no BlackHole; you're not Mechus)

You want **the bot to join as its own participant** — you don't join as Mechus.

**On Mac only:** Not possible. The Zoom Meeting SDK is Linux-only in Mechus; on Mac, `join_backend=zoom_sdk` is unavailable. Your only local option on Mac is the "you join + BlackHole" flow above.

**On Linux (or a Linux VM on your Mac):** The bot can join by itself. No hosting on the internet.

1. Use a **Linux** machine or **Linux VM** (e.g. Ubuntu 22.04 in UTM, Parallels, or VirtualBox). Run Mechus there (Docker or native; see RUN_MECHUS_DOCKER.md for Zoom SDK Docker).
2. In Zoom Marketplace create a **Meeting SDK** app; set `ZOOM_APP_CLIENT_ID` and `ZOOM_APP_CLIENT_SECRET` in `.env` on the Linux host.
3. Start Mechus on Linux (e.g. `./START_SERVER.sh` or Docker Zoom stack).
4. Trigger join (from your Mac or any machine on the same network):
   ```bash
   curl -X POST http://<linux-ip>:5000/api/join-meeting \
     -H "Content-Type: application/json" \
     -d '{"meeting_link":"https://zoom.us/j/YOUR_MEETING","join_backend":"zoom_sdk"}'
   ```
   Use the Linux host's IP (e.g. `192.168.x.x`).
5. Join the Zoom meeting yourself as a **normal participant** (same or different device). When the bot joins, the **host** must click **Allow** when Zoom asks to allow recording. The bot is then in the meeting; you're not Mechus, the bot is.

### On Linux (no Zoom SDK bot locally on Mac)

Zoom SDK bot that *joins* the meeting for you only runs reliably on **real Linux**. So “local without hosting” on a Mac means the **external** path above (you join, BlackHole-style routing isn’t on Linux by default). On a **Linux** machine you can run the Zoom SDK stack locally (no hosting) and have the bot join; use the same Docker Compose or API with `join_backend=zoom_sdk` and a meeting link.

---

## Option A: Docker (recommended even for dev)

Run Mechus the same way as in production:

```bash
./docker-compose-up.sh up -d --build
# API at http://localhost:5001
```

Use `join_backend=external` and join the meeting yourself, or use the Zoom SDK stack for auto-join.

---

## Option B: Run API server on your machine

For quick iteration on API or dashboard without rebuilding Docker:

1. **Python 3.11+**, venv, and deps:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Vosk model** (wake word):
   ```bash
   python3 scripts/download_vosk_model.py --out models
   ```
3. **`.env`** with at least `MECHUS_VOSK_MODEL_PATH=./models/vosk-model-small-en-us-0.15`, TTS/LLM keys as needed.
4. **Start server:**
   ```bash
   ./START_SERVER.sh
   ```
   API at **http://localhost:5000**. Use `join_backend=external` when joining; the server will not open a browser or have audio unless you route it (e.g. BlackHole).

---

## Option C: Full local audio (Mac only, dev/demo)

To have Mechus listen and speak on your Mac (virtual audio loop):

- Install **BlackHole 2ch** and set Zoom/Meet mic/speaker to use it; set `MECHUS_INPUT_DEVICE` and `MECHUS_OUTPUT_DEVICE` in `.env`.
- See **SETUP_MACOS.txt** in the repo root for step-by-step audio routing.
- Run: `python3 scripts/zoom_demo.py` (with venv and `.env` loaded).

This setup is **not** scalable or suitable for production.
