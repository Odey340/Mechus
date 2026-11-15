# Scalable Bot Architecture (30–50 Bots per Server)

If you don't build it this way, you either burn money (1 server per bot) or bots randomly die under load.

**Goal:** 30–50 concurrent meetings per server with Zoom SDK bots, with STT/LLM/TTS external.

That requires: **(1) process isolation**, **(2) tight resource caps**, **(3) a scheduler**, and **(4) a separate "brain" service so the bot process stays dumb and stable.

---

## Core principle

**The Zoom bot process must do almost nothing.**

It should only:

- Join Zoom  
- Stream audio out (PCM to Zoom virtual mic)  
- Receive audio in (raw PCM from Zoom)  
- Keep a heartbeat  
- Forward bytes to your backend  

Everything else (STT, LLM, TTS, wake word, context) lives elsewhere.

---

## Topology

```
              ┌───────────────────────────┐
              │        Mechus API           │
              │  (scheduler + sessions)   │
              └───────────┬───────────────┘
                          │
        assigns meetings  │
                          ▼
┌─────────────────────────────────────────────────┐
│              Bot Host (Ubuntu 22.04)             │
│                                                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐          │
│  │ bot #1  │  │ bot #2  │  │ bot #3  │  ...     │
│  └────┬────┘  └────┬────┘  └────┬────┘          │
│       │            │            │                │
│   PCM in/out   PCM in/out   PCM in/out           │
│       │            │            │                │
│       └──────┬─────┴─────┬─────┘                │
│              ▼           ▼                       │
│         WebSocket to Mechus API                    │
└─────────────────────────────────────────────────┘
```

Mechus API streams audio to:

- STT provider  
- OpenAI LLM  
- ElevenLabs TTS  

and streams back PCM chunks to the bot.

**Result:** Bots are lightweight; concurrency becomes realistic.

---

## Why 30–50 is plausible

If you keep bots dumb, per-bot overhead:

| Resource | Per bot (dumb) |
|----------|----------------|
| CPU      | ~0.08–0.20 vCPU |
| RAM      | ~150–300 MB     |

**8 vCPU / 16 GB server:**

- RAM is the cap before CPU.  
- 16 GB / 0.3 GB ≈ 53 bots (ideal).  
- **Real-world safe: 30–40 bots.**

**4 vCPU / 8 GB server:**

- **Safe: 10–18 bots.**

So 30–50 per server is real **only** with the architecture above (dumb bot + brain elsewhere).

---

## The 4 things you must implement

### 1) Bot runner (stateless)

Each meeting bot runs as a **separate process or container** with:

- `meeting_id`  
- Join token / SDK creds  
- WebSocket URL to Mechus API  
- Minimal logging  

**No** DB, OpenAI key, ElevenLabs key, or STT logic in the bot.

If the bot crashes, it restarts and reconnects.

### 2) Session orchestrator (scheduler)

A small service in Mechus API that:

- Tracks bot hosts  
- Assigns new meetings to the least-loaded host  
- Enforces max concurrency per host  
- Detects dead bots  

**Data model (minimum):**

- `bot_hosts(host_id, region, capacity, active_count, last_heartbeat)`  
- `sessions(session_id, meeting_id, host_id, status, started_at)`  

**Algorithm:** Choose host with smallest `active_count / capacity` → allocate → start bot on that host.

### 3) Host agent (starts bots on the host)

A daemon on each bot-host server that exposes:

- `POST /spawn` — start a new bot container  
- `POST /kill` — kill one  
- `GET /health` — capacity, CPU, RAM, active bots  

Can be Node/Go/Python. Avoids SSH and manual management.

### 4) Resource caps (non-negotiable)

Without caps, one runaway process kills the whole server.

**Per bot container (Docker):**

- `--cpus="0.25"`  
- `--memory="512m"`  
- `--pids-limit=256`  

Without Docker, use systemd slices.

---

## Concrete setup: one server, many bots

### Step A — Server size

- **Hetzner CPX41:** 8 vCPU / 16 GB — best value for 25–40 concurrent.  
- **Hetzner CPX31:** 4 vCPU / 8 GB — only if you're fine with 10–18 concurrent.

### Step B — Docker layout on host

- `mechus-host-agent` (1 container)  
- Bot containers (N)

### Step C — Bot container interface

**Env vars:**

- `MEETING_ID`  
- `RECORDING_TOKEN` (optional)  
- `MECHUS_WS_URL` (required)  
- `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` (or SDK auth material)  

**WebSocket to Mechus API:**

- **Uplink:** PCM frames + events (join, leave, participants)  
- **Downlink:** PCM frames to play + "mute/unmute/speak now" events  

---

## Latency and correctness: failure modes

### A) Backpressure

When STT/LLM/TTS stalls, audio queues grow. You must:

- Drop old audio frames (never buffer infinitely)  
- Cap queue length (e.g. 1–2 seconds max)  
- Degrade gracefully (stop speaking if behind)  

### B) TTS must be chunked/streaming

If you generate full WAV then send, you add 1–2 seconds. You need:

- ElevenLabs **streaming** output  
- Immediate chunk forwarding to Zoom SDK  

### C) Turn-taking gate

- VAD (voice activity detection)  
- "Silence window" (e.g. 400 ms)  
- "Barge-in policy" (never interrupt unless urgent)  

---

## Practical numbers (with your stack)

Assuming:

- STT external (streaming or chunked)  
- LLM = OpenAI  
- TTS = ElevenLabs (streaming when possible)  
- Bot process = lightweight (Zoom + WebSocket only)  

| Server           | Specs        | Safe concurrent |
|------------------|-------------|------------------|
| CPX31            | 4 vCPU, 8 GB  | 10–18            |
| CPX41            | 8 vCPU, 16 GB | 25–40            |
| Dedicated 16/32  | 16 vCPU, 32 GB| 50–80            |

If you claim 50 on 4/8, you're wrong.

---

## What to build first (fastest path)

1. **Host agent** that can spawn bot containers  
2. **Orchestrator** that assigns sessions to hosts  
3. **Hard resource caps** per bot  
4. **WebSocket audio bridge** (bot ⇄ API)  

That gets you multi-meeting on one server in days, not weeks.

---

## Current Mechus codebase: answers (so you don’t design wrong)

### 1. One process per meeting, or multiplexed in one process?

**Multiplexed in one process.** Today the API server runs **one process**; each meeting is a **thread** (`threading.Thread`) in that process. All sessions (Zoom SDK, agent, STT, LLM, TTS) live in the same memory space. So you **cannot** get 30–50 meetings on one box with the current design — one heavy agent per thread will exhaust CPU/RAM long before that.

To hit 30–50 per server you must move to **one process (or container) per meeting** (dumb bot) and move STT/LLM/TTS to a separate “brain” service.

### 2. Zoom SDK bot: Linux-native or Mac Docker in the chain?

**Zoom SDK is Linux-only in code** (`zoom_sdk_available()` checks for Linux + `zoom_meeting_sdk`). So the **bot** is always a Linux process. The open question is **where** that runs:

- **Docker on Mac:** That’s emulated Linux; raw audio callbacks are unreliable (often never fire). So “Mac Docker anywhere in the chain” = not production for Zoom audio.  
- **Real Linux host (VM or cloud):** This is where the bot must run for reliable Zoom SDK audio.

So: bot must be **Linux-native on a real Linux host**; no Mac in the bot path.

### 3. Streaming STT + streaming ElevenLabs, or “send full file then play”?

**Currently “send full then play” (and chunked STT, not streaming).**

- **TTS:** `ElevenLabsTTSEngine.synthesize(text)` does one HTTP POST with full text, reads full response (WAV), then the agent calls `audio_out.play(audio_f32, ...)`. So full WAV is generated then played — **no** ElevenLabs streaming.  
- **STT:** Audio is accumulated in `TranscriptChunker`; when a chunk is ready it’s sent to the transcriber (e.g. FasterWhisper or OpenAI Whisper API) as a batch; you get back a full transcript for that chunk. So **chunked batch STT**, not word-by-word streaming.

To scale and keep latency low you should move to **streaming ElevenLabs** (chunked output → forward to Zoom immediately) and, if possible, **streaming STT** (partial results) so the brain can react faster.

---

## Next: sizing table and Docker Compose

With the three answers above, the **exact server sizing** for the **current** code (one process, many threads, full TTS per response) is:

- **4 vCPU / 8 GB:** ~**2–4** concurrent meetings (each agent does STT + LLM + full TTS in-process).  
- **8 vCPU / 16 GB:** ~**6–10** concurrent.

So today you are **far** from 30–50 per server. To get there you must implement: dumb bot (one process/container per meeting) + WebSocket bridge + orchestrator + host agent + resource caps, as in this doc.

A **Docker Compose + host-agent spawn design** that matches this architecture can be added as a follow-up (e.g. `docker-compose.bot-host.yml` and a small host-agent service).
