# FaceTime via the Chert FaceTime API (`join_backend=facetime_chert`)

Koov joins FaceTime calls through [Chert](https://trychert.com/facetime) instead of driving
FaceTime-on-the-web in Chromium. This replaces the `facetime_web` worker (Selenium + Xvfb +
PulseAudio, one Docker container per call) with a plain Python subprocess.

The old `facetime_web` backend is still in the repo as a fallback (see [FACETIME_JOIN.md](FACETIME_JOIN.md)).

## How it works

Chert's FaceTime API is "bring your own room":

1. `koov/chert_worker.py` joins a LiveKit room and publishes Koov's audio track.
2. It calls `POST https://facetime.trychert.com/api/v1/calls` with the destination, the
   LiveKit URL and a room-scoped token for Chert's bridge.
3. Chert's bridge joins the room and carries the FaceTime call. The caller's audio arrives
   as the `facetime-remote-audio` track; whatever Koov publishes is heard by the caller.
4. `KoovAgent` runs unchanged on that audio (wake word, ElevenLabs STT/TTS, responder,
   memory, summaries). When the call ends or `/api/stop` is called, the worker ends the
   Chert call and writes the final summary.

| File | Purpose |
|---|---|
| `koov/chert_client.py` | Chert Calls API client, LiveKit token minting, destination parsing |
| `koov/chert_livekit.py` | LiveKit room session + audio in/out classes for `KoovAgent` |
| `koov/chert_worker.py` | One-call worker process (spawned by `api_server.py`) |

## Setup

```bash
pip install -r requirements.txt -r requirements-chert.txt
```

Add to `.env`:

```bash
# Chert dashboard -> Developer -> Project API keys (scopes: facetime:read, facetime:write)
CHERT_API_KEY=ftk_...
CHERT_PROJECT_ID=<project UUID>
CHERT_LINE_ID=<line UUID>          # GET /v1/lines, or the dashboard

# Your LiveKit project (livekit.io)
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
```

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `KOOV_FACETIME_BACKEND` | auto | `chert` = always use Chert (also upgrades clients that send `join_backend=facetime_web`); `web` = always use the Chromium worker. Auto = Chert when its credentials are set. |
| `KOOV_CHERT_ANSWER_TIMEOUT_S` | `120` | How long to wait for the call to be answered. |
| `KOOV_CHERT_DESTINATION_TYPE` | inferred | Force `phone`, `email` or `facetime_link`. |
| `CHERT_API_BASE` | `https://facetime.trychert.com/api` | API base URL. |

Every variable above may also be written with a `KOOV_` prefix (e.g. `KOOV_CHERT_API_KEY`).

## Start a call

Call a phone number or Apple ID email (works on any outbound-enabled line):

```bash
curl -X POST http://localhost:5000/api/join-meeting \
  -H 'Content-Type: application/json' \
  -d '{"destination": "+15551234567", "join_backend": "facetime_chert"}'
```

Join an existing FaceTime link (experimental on Chert; must be enabled for your line):

```bash
curl -X POST http://localhost:5000/api/join-meeting \
  -H 'Content-Type: application/json' \
  -d '{"meeting_link": "https://facetime.apple.com/join#...", "join_backend": "facetime_chert"}'
```

`join_backend` can be omitted: phone numbers and emails always go to Chert, and FaceTime
links go to Chert whenever its credentials are configured.

Status and stop are the same endpoints as every other backend:

```bash
curl "http://localhost:5000/api/status?meeting_id=<meeting_id>"
curl -X POST http://localhost:5000/api/stop -H 'Content-Type: application/json' \
  -d '{"meeting_id":"<meeting_id>"}'
```

| `status` | `facetime_state` | `zoom_join_state` | Meaning |
|---|---|---|---|
| `joining` | `opening_link` | `connecting_room` / `dialing` | Joining LiveKit, asking Chert to place the call |
| `joining` | `joining` / `waiting_for_admit` | `ringing` / `bridge_joined` | Ringing, or waiting to be let into the link |
| `active` | `in_call` | | Caller audio is flowing; Koov is listening |
| `stopped` | `ended` / `stopped` | | Caller hung up / Koov was stopped |
| `error` | `error` | | See `worker_error` |

## Video: Koov sees the caller's camera and publishes its own tile

When `KOOV_CHERT_VIDEO=1` (or inspection mode is enabled), Koov publishes a local
portrait video track named `card` before Chert places the call. The first frame is a
Pillow-rendered status card showing the issue count (initially zero). During an inspection,
the count refreshes as new findings are recorded. This is the initial "show" surface; the
move-in reference image and annotated live frame are a later UI step.

Set `KOOV_CHERT_VIDEO=1` (or send `"video": true` to `/api/join-meeting`) to place a
FaceTime Video call. Koov subscribes to `facetime-remote-video`, keeps the newest frame,
trims the black bars Chert adds around portrait video, and answers every question from
that frame with an OpenAI vision model (`koov/vision.py`, `KOOV_VISION_MODEL`,
`KOOV_VISION_ALWAYS=0` to use the camera only for "what do you see"-style questions).
Each frame shown to the model is saved as `koov_notes/chert-vision-<meeting>-NN.jpg`.

## Move-in / move-out inspection

`KOOV_INSPECT_MODE=move_in|move_out` (or `"inspect_mode"` + `"inspect_unit"` in the
`/api/join-meeting` body) turns the call into a walkthrough inspection (`koov/inspection.py`):

- There is no wake word and no transcript agent on these calls. Koov speaks on its own.
- Every 3 s (`KOOV_INSPECT_INTERVAL_S`) the newest frame goes to Gemini Flash
  (`GEMINI_API_KEY`, `KOOV_GEMINI_MODEL`), which returns JSON: surface, issue, severity,
  bounding box and one sentence to speak, or an empty list for a clean frame.
- New findings are spoken through the ElevenLabs TTS path. A finding is announced once.
- `move_in` saves frames and findings to MongoDB (`MONGODB_URI`, collection
  `inspection_frames`), keyed by `KOOV_INSPECT_UNIT`.
- `move_out` loads that unit's move-in frames, sends up to five of them alongside the
  live frame, and asks Gemini which one shows the same area and what damage is new.
- A report is written to `koov_notes/inspection-<meeting>.json`, and `/api/status`
  includes an `inspection` object with the findings so far.

Without `GEMINI_API_KEY` the frames go to OpenAI vision instead; without `MONGODB_URI`
they are saved under `koov_notes/inspections/<unit>/`. Both fallbacks log a warning.

```bash
KOOV_INSPECT_MODE=move_in KOOV_INSPECT_UNIT=apt-4b KOOV_MEETING_ID=in-1 KOOV_MEETING_LINK=+15551234567 python -m koov.chert_worker
```

## Run the worker directly (no API server)

```bash
KOOV_MEETING_ID=test-1 KOOV_MEETING_LINK=+15551234567 python -m koov.chert_worker
```

## Notes and limits

- Koov publishes a Pillow-rendered status card as its own video tile. It does not yet
  publish the side-by-side move-in reference/live camera view or damage bounding boxes.
- **One call per line.** If the line is busy Chert queues the request; the worker reports
  that as an error rather than waiting. Multi-bot invites (`bots`) are not supported.
- **Group calls / links are one mixed audio track**, so there are no per-speaker streams.
- **Echo.** The caller's audio can contain Koov's own voice. Raw-VAD barge-in is off; only
  an explicit "Koov, stop" in the transcript interrupts a reply (same as `facetime_web`).
- **Chert response shapes are partly undocumented** (call status values, event list). The
  worker treats the bridge's audio track appearing in the room as "answered" and the bridge
  leaving as "call ended", and only uses `GET /v1/calls/{id}` as a best-effort early-failure
  signal.
- A 201 from `POST /v1/calls` means *allocated*, not answered.
