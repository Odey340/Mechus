# Koov – Production meeting intelligence agent

Koov is a **production-grade** meeting agent that joins calls, transcribes, answers wake-word questions with grounded responses, and scales to many concurrent meetings. It is designed to run in **Docker and cloud**—not as a local desktop app.

- **Scale:** Run multiple containers/instances behind a load balancer with sticky sessions (e.g. 100+ meetings).
- **Deploy:** Docker first; single image for API + Zoom SDK join (Linux). See [docs/PRODUCTION.md](docs/PRODUCTION.md) and [docs/RUN_KOOV_DOCKER.md](docs/RUN_KOOV_DOCKER.md).
- **Vercel dashboard:** The Mechus landlord UI can be hosted on Vercel while the meeting and messaging services remain on the container backend. See [docs/VERCEL.md](docs/VERCEL.md).
- **No local run for production:** For production, always use Docker (or container/orchestrator of choice). Local run is for **development only** (see below).

---

## Quick run (one script)

From the repo root:

| Command | What it does |
|--------|----------------|
| `./run.sh local` | Start the API server locally (development) |
| `./run.sh docker` | Run Koov in Docker with Zoom SDK (Mac/Linux) |
| `./run.sh deploy` | Sync code to your VM and restart the container |

**Deploy once:** Copy `.deploy.env.example` to `.deploy.env`, set `DEPLOY_HOST=ubuntu@YOUR_VM_IP` and optionally `DEPLOY_SSH_KEY=path/to/key.pem`. Then `./run.sh deploy` does sync + restart in one go. See [docs/DEPLOY_API_CHANGES.md](docs/DEPLOY_API_CHANGES.md).

---

## How to run (production)

### 1. Docker (recommended)

From the repo root:

```bash
# Default: API only (join_backend=external; you or your app route audio)
./docker-compose-up.sh up -d --build
```

- API: **http://localhost:5001** (host port; container uses 5000).
- Check: `curl http://localhost:5001/health` → `{"status":"ok",...}`.

### 2. Docker with Zoom SDK (Koov joins meetings itself)

Koov can join Zoom meetings from inside the container (no browser, full audio):

```bash
# One-time: set ZOOM_APP_CLIENT_ID and ZOOM_APP_CLIENT_SECRET in .env
./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

Then have your dashboard or backend call:

```bash
POST http://localhost:5001/api/join-meeting
Content-Type: application/json
{"meeting_link":"https://zoom.us/j/MEETING?pwd=PWD","user_id":"user-1"}
```

Koov joins as "Koov"; host may need to allow recording when Zoom prompts. See [docs/ZOOM_SDK_SETUP.md](docs/ZOOM_SDK_SETUP.md).

### 3. Scaling (e.g. 100 meetings)

- **Multiple instances:** Run more containers or VMs behind a load balancer. Sessions are in-memory per process; use **one worker per process** (no multi-worker scaling within one container).
- **Sticky sessions (required):** All requests for the same meeting/user must hit the same instance. Configure session affinity on your load balancer (cookie or header).
- **Secrets & CORS:** Set `KOOV_API_KEY`, `KOOV_CORS_ORIGINS`, and inject Zoom/TTS/LLM keys via your platform. See [docs/PRODUCTION.md](docs/PRODUCTION.md).

---

## Tiger Data + Vultr (HackUTA)

- **Tiger Data:** every transcript line, decision/action-item snapshot and answer latency is mirrored into TimescaleDB hypertables on Tiger Cloud. You get speaker talk share, activity timelines, cross-meeting search and p95 latency with plain SQL (`/api/meetings/<id>/analytics`, `/api/analytics/overview`, `/api/tiger/search`). Set `TIGER_DATABASE_URL`, then run `python scripts/tiger_init.py --demo`. See [docs/TIGER_DATA.md](docs/TIGER_DATA.md).
- **Vultr:** `python scripts/vultr_deploy.py create` + `./run.sh deploy` (or just `./run.sh vultr`) puts Koov on a Vultr Cloud Compute server in Dallas. `KOOV_LLM_PROVIDER=vultr` routes answers through Vultr Serverless Inference. See [docs/VULTR.md](docs/VULTR.md).
- **Windows dev setup:** `setup_windows.bat`

---

## What Koov does

- Joins meetings via Zoom SDK (Linux container) or external/browser join
- Joins FaceTime calls via the Chert FaceTime API (see [docs/CHERT_FACETIME.md](docs/CHERT_FACETIME.md))
- Listens; wakes only on a wake word (e.g. `computer`, `koov`)
- Transcribes with faster-whisper (or cloud STT)
- Answers with grounded responses (OpenAI or rule-based), cites evidence
- Speaks via ElevenLabs TTS
- Writes session notes (JSONL) and exposes live/final transcript over API

---

## Repo layout

| Path | Purpose |
|------|--------|
| `koov/api_server.py` | HTTP API (join, status, stop, transcript) |
| `koov/agent.py` | Main agent (wake, STT, responder, TTS, memory) |
| `koov/zoom_sdk_join.py` | Zoom SDK join (Linux); audio in/out |
| `koov/chert_worker.py` | FaceTime call via Chert + LiveKit (`join_backend=facetime_chert`) |
| `docker-compose.yml` | Production-style API service |
| `docker-compose.zoom.yml` | Override for Zoom SDK join |
| [docs/PRODUCTION.md](docs/PRODUCTION.md) | Security, config, scaling, checklist |
| [docs/RUN_KOOV_DOCKER.md](docs/RUN_KOOV_DOCKER.md) | Run options, API examples |

---

## Development / testing only

Koov is **not** intended to run as a long-lived local process in production. For quick dev or smoke tests you can run the API server on your machine:

```bash
# Dev only: run API server on this machine
./START_SERVER.sh
# Then: POST http://localhost:5000/api/join-meeting with join_backend=external
```

**How does Koov hear the meeting?** In production, Koov joins by itself (Zoom SDK or RTMS); you don’t join for it. See **[docs/RUN_KOOV_HEAR_ME.md](docs/RUN_KOOV_HEAR_ME.md)**. For full local testing (virtual audio, BlackHole, zoom_demo), see [docs/DEV_LOCAL_SETUP.md](docs/DEV_LOCAL_SETUP.md) (optional). Production deployments must use Docker or equivalent and scale horizontally.

---

## Configuration (.env)

Copy `.env.example` to `.env` and set at least:

- **API / production:** `KOOV_API_KEY`, `KOOV_CORS_ORIGINS` (when exposed).
- **Wake word:** `KOOV_WAKE_PHRASE` (e.g. `computer`), `KOOV_VOSK_MODEL_PATH` (Vosk model; Docker image includes it).
- **TTS:** `KOOV_TTS_BACKEND=elevenlabs`, `KOOV_ELEVEN_API_KEY`, `KOOV_ELEVEN_VOICE_ID`.
- **LLM:** `KOOV_LLM_BACKEND=openai`, `KOOV_OPENAI_API_KEY`.
- **Zoom SDK join:** `ZOOM_APP_CLIENT_ID`, `ZOOM_APP_CLIENT_SECRET`.
- **FaceTime (Chert):** `CHERT_API_KEY`, `CHERT_PROJECT_ID`, `CHERT_LINE_ID`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.

All behavior is driven by environment variables so you can deploy and scale without code changes.
