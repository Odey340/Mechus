# FaceTime-on-web worker backend (Linux + Docker)

> **Superseded:** FaceTime calls now go through the Chert FaceTime API by default when it is
> configured. See [CHERT_FACETIME.md](CHERT_FACETIME.md). This browser-based backend remains as a
> fallback (`join_backend=facetime_web`, or `MECHUS_FACETIME_BACKEND=web`).

Mechus cannot join FaceTime via an SDK (FaceTime has no bot API). This backend joins **FaceTime on the web** using a FaceTime link in Chromium, and routes audio via a per-container PulseAudio graph so Mechus can hear/speak in the call.

Apple's requirements for FaceTime on the web: Chrome/Edge and **host must admit** the participant. See Apple Support: `https://support.apple.com/en-us/109364`.

## How it works

- API server spawns **one Docker container per meeting** when `join_backend=facetime_web`.
- The worker container runs:
  - **PulseAudio** with two null sinks (`call_sink`, `mechus_sink`) and one remapped source (`mechus_mic_source`)
  - **Xvfb** (virtual display)
  - **Chromium + chromedriver** (Selenium) to open the FaceTime link and click Join
  - `python -m mechus.facetime_worker` (MechusAgent using sounddevice `pulse`)

### Audio routing

- Chromium:
  - Speaker output → `call_sink`
  - Microphone input ← `mechus_mic_source` (which is `mechus_sink.monitor`)
- Mechus:
  - Input source = Pulse default source = `call_sink.monitor`
  - Output sink = Pulse default sink = `mechus_sink`

This is fully automatic: **no manual patching** of audio devices per meeting.

### Startup parallelism

MechusAgent (Vosk/STT/TTS model loading, 15–60 s) now starts in a **background thread** as soon as Chromium launches. The main thread simultaneously navigates to the FaceTime link, enters the display name, and clicks Join. By the time the host taps Admit, model loading is usually done — eliminating dead air between admit and first transcript.

## Build the worker image

`scripts/restart-on-server.sh` (used by `./run.sh deploy`) runs this on the VM before starting the API container. You can also build manually from repo root:

```bash
docker build -t mechus-facetime-worker:latest -f docker/facetime-worker/Dockerfile .
```

The API server runs a preflight `docker image inspect` before spawning a container and returns HTTP 503 with a build hint if the image is missing.

## Run the API server with Docker access

If you run the API server itself in Docker, it must be able to spawn worker containers. The common approach is mounting the Docker socket:

```bash
docker run --rm -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/mechus_notes:/app/mechus_notes" \
  -e MECHUS_NOTES_DIR=/app/mechus_notes \
  -e "MECHUS_NOTES_HOST_PATH=$(cd mechus_notes && pwd)" \
  -e MECHUS_FACETIME_WORKER_IMAGE=mechus-facetime-worker:latest \
  mechus-api:latest
```

`MECHUS_NOTES_HOST_PATH` must be the **host** absolute path that backs `/app/mechus_notes` inside the API container, because `docker run -v` is resolved by the host daemon.

Also ensure the API server can see your `.env` file path (it passes it to `docker run --env-file` when present).

### Repo deploy (`docker-compose.yml` + `docker-compose.zoom.yml`)

This repo's Zoom deploy compose file mounts the host Docker socket and adds the host docker group gid:

- `docker-compose.zoom.yml` includes:
  - `- /var/run/docker.sock:/var/run/docker.sock`
  - `group_add: - ${DOCKER_GID}` (**required** — do not default this to `0`)

Set `DOCKER_GID` in your server `.env` to match the socket group on the VM:

```bash
stat -c '%g' /var/run/docker.sock
```

`scripts/restart-on-server.sh` will auto-export `DOCKER_GID` from the socket when it is not already set.

Notes are bind-mounted from the host via `MECHUS_HOST_NOTES_DIR` (defaults to `./mechus_notes` relative to the compose project directory). `scripts/restart-on-server.sh` resolves that to an absolute path and exports `MECHUS_NOTES_HOST_PATH` into the API container so `docker run -v "$MECHUS_NOTES_HOST_PATH:/notes"` targets the same directory on the host.

The `mechus-zoom` image includes the **Docker CLI** (client only) so `api_server.py` can run `docker …`.

## Start a FaceTime meeting (via link)

Call:

```bash
curl -X POST http://localhost:5000/api/join-meeting \
  -H 'Content-Type: application/json' \
  -d '{
    "meeting_link": "https://facetime.apple.com/join#....",
    "display_name": "Mechus",
    "join_backend": "facetime_web"
  }'
```

The worker will reach **waiting_for_admit** once it has clicked Join and the host needs to tap Admit.

## Check status

```bash
curl "http://localhost:5000/api/status?meeting_id=<meeting_id>"
```

Important fields in the JSON response:

| Field | Meaning |
|---|---|
| `status` | `joining \| active \| stopped \| error` |
| `facetime_state` | `opening_link \| waiting_for_name \| waiting_for_join_click \| waiting_for_admit \| in_call \| error` |
| `facetime_join_clicked` | `true` once the Join/Continue button was clicked (knock sent to host) |
| `facetime_ready` | `true` while the join knock has been sent and Mechus is still waiting for the host to tap Admit; clears after admission |
| `facetime_mic_ok` | `true` once Chromium's mic track is routed to mechus_mic_source |
| `facetime_pulse_rewire_ok` | `true` once PulseAudio move-source-output succeeded |

### Correct polling loop

**Always** poll `GET /api/status` until `facetime_state` is `waiting_for_admit` (or `in_call`) before declaring join failure. The container needs at least 30–90 s to start Chrome, navigate, enter name, and click Join. A typical timeline:

| Time | State | facetime_ready |
|---|---|---|
| 0–5 s | `opening_link` | — |
| 5–20 s | `waiting_for_name` / `waiting_for_join_click` | — |
| 20–60 s | `waiting_for_admit` | `true` |
| after admit | `in_call` | `true` |

### Do NOT stop early

Do **not** call `POST /api/stop` while `facetime_state` is still `joining`/`waiting_for_name` unless you pass `"force": true`. An early stop races with the browser phase and removes the container before the knock reaches Apple. The server guards against this for the first 90 s by default via `_facetime_early_stop_blocked` (`MECHUS_FT_EARLY_STOP_GUARD_S` can override it), but cooperative polling is better.

## Stop a meeting

```bash
curl -X POST http://localhost:5000/api/stop \
  -H 'Content-Type: application/json' \
  -d '{"meeting_id":"<meeting_id>"}'
```

Pass `"force": true` to bypass the early-stop guard if you genuinely need to abort during startup.

## Camera and visual questions (optional)

Both features are **off by default**. If they are off or fail, the bot joins and runs audio-only exactly as before. Host setup is covered in [FACETIME_CAMERA_RUNBOOK.md](FACETIME_CAMERA_RUNBOOK.md).

| Setting | Default | Meaning |
|---|---|---|
| `MECHUS_FT_CAMERA_FEATURE` | `0` | Server-wide switch. When `1`, every FaceTime worker gets its own virtual camera device (if one is free). |
| `MECHUS_FT_VIDEO_DEVICES` | — | Device pool, e.g. `/dev/video20,/dev/video21`. One device per concurrent bot. |
| `MECHUS_FT_CAMERA_AUTO_ON` | `0` | Initial camera state when the join request doesn't say. |
| `MECHUS_FT_CAMERA_REQUIRED` | `0` | Fail the join instead of falling back to audio-only. |
| `MECHUS_FT_CAMERA_DEFAULT_MODE` | `avatar` | `avatar`, `activity`, `listening`, `thinking`, `speaking`, `status_card`, `message_card`, `off`. |
| `MECHUS_FT_VIDEO_WIDTH` / `_HEIGHT` / `_FPS` | `640` / `360` / `12` | Generated stream format. |
| `MECHUS_FT_VISION_ENABLED` | `0` | Server-wide switch for visual questions. Meetings must also opt in. |
| `MECHUS_OPENAI_VISION_MODEL` | `gpt-4o-mini` | Vision-capable model. |
| `MECHUS_FT_VISION_DETAIL` | `auto` | `low` / `high` / `auto` image detail. Use `high` for small whiteboard text. |
| `MECHUS_FT_VISION_MAX_REQUESTS_PER_MINUTE` | `3` | Per-meeting limit (API and voice combined per worker). |

Set `MECHUS_API_KEY` whenever either feature is on.

### Join request

```json
{
  "meeting_link": "https://facetime.apple.com/join#...",
  "camera": {"enabled": true, "required": false, "mode": "activity"},
  "vision": {"enabled": true}
}
```

`camera.enabled` sets only the *initial* state. A provisioned meeting can turn the camera on or off later. The response echoes `camera`, `vision`, and any `warnings` (for example, the feature is off on this server, or settings were ignored for a non-FaceTime link). If the device pool is exhausted, the worker joins audio-only and reports `facetime_camera_error_code=camera_pool_exhausted`.

`activity` mode switches automatically between listening, thinking, and speaking based on what Mechus is doing.

### Camera control

```bash
curl -X POST http://localhost:5000/api/meetings/<meeting_id>/camera \
  -H 'Content-Type: application/json' -H "X-API-Key: $MECHUS_API_KEY" \
  -d '{"action":"show_card","mode":"status_card","text":"Mechus is preparing the summary","duration_seconds":10}'
```

| Action | Body |
|---|---|
| Turn on | `{"action":"turn_on"}` |
| Turn off | `{"action":"turn_off"}` |
| Change mode | `{"action":"set_mode","mode":"activity"}` |
| Temporary card | `{"action":"show_card","mode":"status_card"\|"message_card","text":"≤200 chars","duration_seconds":1–60}` |

Responses: `202` accepted, `400` invalid, `404` unknown meeting, `409` not FaceTime (`camera_not_facetime`) or no device (`camera_not_provisioned`), `503` feature off. For multi-bot meetings, use each `bot_meeting_id`.

**202 means accepted, not applied.** Poll `/api/status` until `facetime_camera_control_revision` reaches the returned `camera.revision`, then compare `facetime_camera_enabled` with what you requested. The worker repairs drift (for example, someone toggles the camera in FaceTime) every ~10 s.

### Visual questions

From voice: *"Mechus, look at the whiteboard"*, *"Mechus, read what I'm holding"*. A command must contain both a looking verb and a visible target, so "show me the action items" is answered normally.

From the API:

```bash
curl -X POST http://localhost:5000/api/meetings/<meeting_id>/vision/query \
  -H 'Content-Type: application/json' -d '{"question":"What is written on the whiteboard?"}'
# -> 202 {"request_id": "...", "status": "queued"}
curl http://localhost:5000/api/meetings/<meeting_id>/vision/requests/<request_id>
# -> {"status": "pending|processing|completed|failed", "answer": "...", "error_code": "..."}
```

Errors: `503` feature off, `409` meeting didn't opt in, `429` rate limited, `400` empty question or longer than 500 characters. The worker captures **one** frame per request, cropped to the largest remote video tile, sends it to the model, and keeps only the text answer. Screenshots are never written to disk. No face identification and no emotion or other sensitive-trait inference.

### Status fields

`/api/status` (and each bot's `camera` object in multi-bot status) includes:
`facetime_camera_feature_enabled`, `facetime_camera_device_assigned`, `facetime_camera_available`, `facetime_camera_producer_ok`, `facetime_camera_selected`, `facetime_camera_enabled` (actual), `facetime_camera_desired_enabled`, `facetime_camera_desired_mode`, `facetime_camera_actual_mode`, `facetime_camera_control_revision`, `facetime_camera_last_frame_at`, `facetime_camera_error_code`, `facetime_camera_error`, `facetime_vision_enabled`, `facetime_vision_busy`, `facetime_vision_last_request_id`, `facetime_vision_last_error`.

Device paths, browser device IDs, and images are never exposed.

## E2E checklist

- [ ] Ensure `mechus-facetime-worker:latest` exists on the Docker host (`restart-on-server.sh` builds it; or `docker build` manually — the API server returns HTTP 503 with a build hint if the image is missing)
- [ ] Start API server with docker socket access
- [ ] Create a FaceTime link from an Apple device and start the call
- [ ] POST `/api/join-meeting` with `join_backend=facetime_web`
- [ ] Poll `GET /api/status` — wait until `facetime_ready=true` or `facetime_state=waiting_for_admit`
- [ ] Approve "Mechus" in the FaceTime waiting room on the Apple device
- [ ] Speak in the call; verify Mechus transcribes within ~5 s and responds on its wake word
- [ ] Check transcript shows `Speaker N:` lines within ~10–30 s if async diarization is enabled

## Notes / limitations

- Host-admit cannot be bypassed; someone must tap Admit on an Apple device.
- DOM/selectors for FaceTime web may change. On join-click failure the worker saves diagnostic artifacts:
  - `mechus_notes/facetime-<meeting_id>-joinclick-miss.png`
  - `mechus_notes/facetime-<meeting_id>-joinclick-miss.html`
- On any fatal error:
  - `mechus_notes/facetime-<meeting_id>-error.png`
  - `mechus_notes/facetime-<meeting_id>-error.html`
- Camera selector/toggle failures save sanitized control markup once per meeting:
  - `mechus_notes/facetime-<meeting_id>-camera-dom.html`
