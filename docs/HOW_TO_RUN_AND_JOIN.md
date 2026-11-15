# How to Run Koov and Join from the Dashboard

This guide covers: (1) how to run Koov, (2) joining a meeting by pasting a link in the dashboard, and (3) **joining without a visible Chrome window** while still having **audio** so Koov can listen to the conversation and take notes and speak responses.

---

## 1. How to Run Koov

### Option A: With the API (for dashboard paste-to-join)

Use this when you want to paste a meeting link in your dashboard and have Koov join automatically.

1. **One-time setup**
   - Create a virtual environment and install dependencies:
     ```bash
     cd /path/to/koovdemo
     python3 -m venv .venv
     source .venv/bin/activate
     pip install -r requirements.txt
     ```
   - Copy and edit `.env` (see [README](README.md) and [SETUP_MACOS](SETUP_MACOS.txt) for audio devices, Vosk, TTS, LLM):
     ```bash
     cp .env.example .env
     # Edit .env: KOOV_INPUT_DEVICE, KOOV_OUTPUT_DEVICE, Vosk path, TTS, LLM, etc.
     ```
   - Install and route **virtual audio** (e.g. BlackHole 2ch) so Koov can hear the meeting and output TTS. See [README](README.md) and `SETUP_MACOS.txt`.

2. **Start the API server**
   ```bash
   source .venv/bin/activate
   export PYTHONPATH="$PWD"
   python3 scripts/start_api_server.py
   ```
   You should see something like: `Starting Koov API server on 127.0.0.1:5000`.

3. **Dashboard → paste link → join**
   - In your dashboard, when the user pastes a meeting link and clicks “Start” (or equivalent), call:
     ```http
     POST http://127.0.0.1:5000/api/join-meeting
     Content-Type: application/json

     {
       "meeting_link": "https://meet.google.com/xxx-xxx-xxx",
       "user_id": "user-123",
       "display_name": "Koov"
     }
     ```
   - By default the server opens the meeting in a browser and starts the Koov agent. For **no browser / scalable** use, send `"join_backend": "external"` and route meeting audio to Koov’s input device separately. See [SCALABLE_JOIN](SCALABLE_JOIN.md) and [QUICK_START_API](QUICK_START_API.md).

### Option B: Standalone (no API, no dashboard)

Use this when you open the meeting yourself and run Koov on the same machine.

```bash
source .venv/bin/activate
python main.py
```

You manually open the meeting in a browser, set Zoom/Meet audio to use BlackHole (or your configured input/output), and Koov listens via the same virtual audio device.

---

## 2. Joining Without a Visible Chrome Window (With Audio)

You need **two things** for note-taking and TTS:

- **Audio input:** Koov must hear the meeting (via a virtual audio device like BlackHole).
- **Audio output:** Koov must be able to play TTS into the meeting (again via virtual audio).

**Important:** In standard **headless** Chrome, the browser does **not** output audio. So if Koov joins via headless Chrome, the meeting tab has no real audio stream — Koov cannot hear the conversation and cannot play TTS into the call. Headless is therefore **not** suitable when you need “listen + take notes + audio output.”

You have two practical options:

### Option 1: Visible Chrome, then minimize (recommended)

- Run the browser **with a visible window** so that Chrome actually plays and captures audio.
- Set in your environment (or `.env`):
  ```bash
  KOOV_SHOW_BROWSER=true
  ```
- When you start a session from the dashboard, Chrome will open and join the meeting. **Minimize the Chrome window** (or move it to another desktop). The meeting keeps running with full audio; Koov can listen and take notes and output TTS.

No code change required; just use `KOOV_SHOW_BROWSER=true` and minimize the window.

### Option 2: No browser at all (Zoom SDK / Meet API)

- Join the meeting **without Chrome** using the platform’s API (e.g. Zoom SDK, or Google Meet API if available). The join and audio are handled by the platform’s native/SDK stack.
- This gives true “no browser” with audio, but requires extra setup (accounts, API keys, and possibly different code paths). See [NO_BROWSER_JOIN](NO_BROWSER_JOIN.md) for an overview.

**Summary**

| Goal                         | Headless Chrome | Visible Chrome (minimize) | Zoom/Meet API   |
|-----------------------------|-----------------|---------------------------|-----------------|
| No visible window           | Yes             | Yes (minimize)            | Yes             |
| Koov hears meeting          | No              | Yes                       | Yes (if wired) |
| Koov can output TTS         | No              | Yes                       | Yes (if wired) |
| Listen + take notes + audio | No              | Yes                       | Yes             |

So: **to join from the dashboard without a visible window but with audio, use visible Chrome and minimize it** (`KOOV_SHOW_BROWSER=true`), or implement a no-browser join via Zoom/Meet API.

---

## 3. Audio So Koov Can Listen and Speak

- **Input:** Set Zoom/Meet **microphone** to the same virtual device Koov uses as input (e.g. `BlackHole 2ch`). That way Koov’s TTS is sent into the meeting.
- **Output:** Set Zoom/Meet **speaker** to a device that includes that same virtual device (e.g. a Multi-Output device that includes `BlackHole 2ch`). That way Koov hears the meeting.
- In `.env` set:
  - `KOOV_INPUT_DEVICE=BlackHole 2ch` (or your virtual input)
  - `KOOV_OUTPUT_DEVICE=BlackHole 2ch` (or your virtual output)

Detailed routing steps are in the main [README](../README.md) and `SETUP_MACOS.txt`.

---

## 4. Quick Reference

| Step | Command or action |
|------|--------------------|
| Start API server | `PYTHONPATH="$PWD" python3 scripts/start_api_server.py` |
| Join from dashboard | `POST /api/join-meeting` with `meeting_link` and optional `display_name` |
| No visible window + audio | `KOOV_SHOW_BROWSER=true` and minimize Chrome after join |
| Headless (no audio) | `KOOV_SHOW_BROWSER=false` — not for listening or TTS |

For full API details, see [QUICK_START_API](QUICK_START_API.md) and [MEETING_AUTOMATION](MEETING_AUTOMATION.md).
