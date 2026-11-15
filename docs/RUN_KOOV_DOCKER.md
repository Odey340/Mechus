# Run Koov (production: Docker and cloud)

Koov is production-grade and scales via Docker (and cloud). **Do not run Koov as a long-lived local process for production.** Use Docker and scale with multiple instances + sticky sessions.

---

## 1. Docker with Zoom SDK (recommended for production)

Koov joins Zoom meetings from inside the container (no browser, full audio). Easiest to scale: run more containers behind a load balancer with session affinity.

To run Koov **in Docker and have it output audio** (Koov joins the meeting from inside the container via Zoom):

1. **One-time:** Put your Zoom app credentials in `.env`:
   ```bash
   ZOOM_APP_CLIENT_ID=your_client_id
   ZOOM_APP_CLIENT_SECRET=your_client_secret
   ```
   (Create a Meeting SDK app at [Zoom Marketplace](https://marketplace.zoom.us/) if needed.)

2. **Start the Zoom SDK stack** (from repo root):
   ```bash
   ./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
   ```
   Or use the convenience script: `./docker-compose-zoom.sh up -d --build`

3. **Join a meeting** from your dashboard or curl (use a **Zoom** meeting link; do not send `join_backend: "external"`):
   ```bash
   curl -X POST http://localhost:5001/api/join-meeting \
     -H "Content-Type: application/json" \
     -d '{"meeting_link":"https://zoom.us/j/MEETING_NUMBER?pwd=PASSWORD","user_id":"user-1"}'
   ```
   Koov joins as "Koov" and gets full audio via the Zoom SDK. If there’s a waiting room, the host must admit "Koov".

---

## 2. Docker default (API only; no audio in container)

From the repo root:

```bash
cd /path/to/koovdemo
./docker-compose-up.sh up --build
```

- First run: build can take a few minutes (Vosk model, deps).
- API is on **port 5001** on your machine (mapped from 5000 in the container).
- Check: `curl http://localhost:5001/health` → `{"status":"ok",...}`

To run in the background:

```bash
./docker-compose-up.sh up -d --build
```

**Note:** This default image uses `KOOV_JOIN_BACKEND=external` and the container has no sound device, so Koov cannot hear or output meeting audio. For Docker with audio, use the **Zoom SDK** stack above.

---

## 3. Join a meeting (external – you join; Koov uses your audio)

Default image uses **`KOOV_JOIN_BACKEND=external`**: Koov does **not** open a browser. You join the meeting yourself; then you tell Koov to start so it uses your configured audio (e.g. BlackHole).

**On your Mac:**

1. **Join the meeting** (Zoom or Google Meet app/browser).
2. **Audio:**
   - **Speaker:** Multi-Output device that includes **BlackHole 2ch** (so meeting audio goes to BlackHole).
   - **Microphone:** **BlackHole 2ch** (so Koov’s TTS is sent into the meeting when Koov speaks).
3. **Start Koov** by calling the API (dashboard or curl):

```bash
curl -X POST http://localhost:5001/api/join-meeting \
  -H "Content-Type: application/json" \
  -d '{"meeting_link":"https://zoom.us/j/YOUR_MEETING?pwd=YOUR_PWD","user_id":"user-1","join_backend":"external"}'
```

- Use your real Zoom/Meet link (with `?pwd=...` if there’s a passcode).
- Response includes `meeting_id`; use it for status/transcript/stop.

**Note:** With the default Docker setup, the Koov **process runs inside the container**, which usually has no access to your Mac’s BlackHole. So for **real audio in/out** when using Docker, either:

- Run the API server **on the host** (not in Docker) and use `join_backend=external` with BlackHole set in `.env`, or  
- Use the **Zoom SDK Docker image** (below) so Koov joins from inside the container and gets audio via Zoom.

---

## 4. Zoom SDK: join and verify (details)

Koov can join the meeting **from inside the container** using the Zoom Meeting SDK (no browser, audio via Zoom).

**One-time:**

1. **Zoom Meeting SDK app**  
   Create an app at [Zoom Marketplace](https://marketplace.zoom.us/) (Meeting SDK). Get **Client ID** and **Client Secret**.

2. **`.env`** in the repo root:
   ```bash
   ZOOM_APP_CLIENT_ID=your_client_id
   ZOOM_APP_CLIENT_SECRET=your_client_secret
   ```

3. **Build and run the Zoom image** (from repo root):
   ```bash
   ./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
   ```

**To join a meeting:**

- From your dashboard or curl, call the API with the **Zoom link** (no need to join yourself):

```bash
curl -X POST http://localhost:5001/api/join-meeting \
  -H "Content-Type: application/json" \
  -d '{"meeting_link":"https://zoom.us/j/MEETING_NUMBER?pwd=PASSWORD","user_id":"user-1"}'
```

- Koov joins as “Koov” (or your `display_name`).  
- **Host:** If Zoom asks to allow recording, allow it so Koov gets audio.  
- **Waiting room:** Host must admit “Koov” if the meeting has a waiting room.

More: [ZOOM_SDK_SETUP.md](ZOOM_SDK_SETUP.md).

### Verify Koov can hear you (Zoom SDK)

After Koov has joined, check whether it’s receiving audio:

1. **Status API** (use the `meeting_id` from the join response):
   ```bash
   curl "http://localhost:5001/api/status?meeting_id=YOUR_MEETING_ID"
   ```
   - **`audio_status`** – single field to see if Koov is detecting audio:
     - `"receiving"` → Koov is getting audio right now (you can try the wake word).
     - `"no_audio_yet"` → Zoom has not sent any frames (host allow recording, or check Zoom app).
     - `"silent"` → Koov got audio before but none in the last 15 seconds.
   - `receiving_audio`, `last_audio_received_at`, `zoom_frames_received`, `no_audio_hint` – extra detail.

2. **Docker logs** – proof that audio is detected (before any words):
   ```bash
   docker logs koovdemo-koov-1 -f
   ```
   - `Zoom audio: first frame received; Koov can hear the meeting` → Zoom sent first frame.
   - `Koov agent: first audio frame received; listening for wake word` → agent is reading audio.
   - `Zoom audio: N frames in last 5s (audio detected)` → repeated every 5s while audio is coming.
   - `No Zoom audio frames received after 30s...` → Zoom isn’t sending audio; see [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md).

---

## 5. Quick reference

| Step | Command / action |
|------|-------------------|
| Start Koov (default) | `./docker-compose-up.sh up --build` |
| API base URL | `http://localhost:5001` |
| Health check | `curl http://localhost:5001/health` |
| Join (external – you joined already) | `POST http://localhost:5001/api/join-meeting` with `meeting_link`, `user_id`, `"join_backend":"external"` |
| Join (Zoom SDK – Koov joins) | Same URL with `meeting_link` (Zoom only); use Zoom Docker image |
| Status | `GET http://localhost:5001/api/status` |
| Stop | `POST http://localhost:5001/api/stop` |

---

## 6. Troubleshooting

- **Permission denied** on `./docker-compose-up.sh` or `./docker-compose-zoom.sh`: run `chmod +x docker-compose-up.sh docker-compose-zoom.sh`.
- **Port 5001 in use:** In `docker-compose.yml` change `"5001:5000"` to e.g. `"5002:5000"` and use port 5002 in the URLs above.
- **“Wake word model not found” (503):** Rebuild: `./docker-compose-up.sh build --no-cache && ./docker-compose-up.sh up -d`. The Dockerfile downloads the Vosk model during build.
- **Zoom image "platform (linux/amd64) does not match host (linux/arm64)":** Expected on Apple Silicon (M1/M2/M3). The Zoom SDK image is amd64-only; Docker runs it via emulation. It may be slower but is supported.
- **Smoke test:** `./scripts/test-docker-api.sh` (uses `KOOV_API_URL=http://localhost:5001` by default).
- **Koov can’t hear me (Zoom SDK):**  
  1. Call `GET /api/status?meeting_id=...` and check `receiving_audio` and `zoom_frames_received`.  
  2. If `zoom_frames_received` is 0, Zoom isn’t sending audio: **host must allow recording** when Zoom prompts after Koov joins.  
  3. Check logs: `docker logs koovdemo-koov-1 2>&1 | grep -E "Zoom audio|first frame|No Zoom audio"`.  
  Full checklist: [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md).
- **Local run (dev only):** To run the API server on your machine instead of Docker, see [DEV_LOCAL_SETUP.md](DEV_LOCAL_SETUP.md). Do not use for production.
