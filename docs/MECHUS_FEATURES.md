# Mechus — Features Overview (Current & Planned)

**Last updated:** May 2026  
**Audience:** Product, engineering, and dashboard integration  
**Repo:** `mechusdemo`

This document summarizes what Mechus does today in production-oriented deployments, what is experimental or partial, and what is explicitly planned but not built yet. It is derived from the codebase, `README.md`, and internal architecture docs.

---

## What Mechus Is

Mechus is a **meeting intelligence agent** that joins calls, transcribes discussion, wakes only when addressed (wake word), answers questions with **meeting-grounded** responses, speaks in a natural voice, and exposes state to your app via HTTP API. It is designed for **Docker on Linux** (Zoom SDK, FaceTime-on-web workers), not as a desktop-only app.

---

## Current Features (Shipped)

### Meeting join & platforms

| Capability | Status | Notes |
|------------|--------|--------|
| **Zoom SDK join** (`join_backend=zoom_sdk`) | ✅ Production path | Linux container; no browser. Host may need to allow recording for raw audio. |
| **FaceTime on web** (`join_backend=facetime_web`) | ✅ | Chromium + PulseAudio per-meeting Docker worker; host must admit participant. |
| **Browser / external join** | ✅ Dev / legacy | Selenium Chrome or external audio routing; not the primary production path. |
| **Auto-infer join backend from URL** | ✅ | Zoom vs FaceTime links detected when `join_backend` omitted. |
| **One meeting per user** (configurable) | ✅ | Enforced at API layer. |
| **Concurrent meetings** | ✅ | Multiple sessions per API process; scale via multiple containers + sticky sessions. |
| **Worker subprocess isolation** | ✅ | Zoom: `mechus.worker`; FaceTime: separate container per meeting. |

### Voice, wake word & turn-taking

| Capability | Status | Notes |
|------------|--------|--------|
| **Wake phrases** | ✅ | Default: `computer`, `mechus`, `hey computer`, etc. (`MECHUS_WAKE_PHRASE`). |
| **Transcript-based wake (Zoom/FaceTime)** | ✅ | Primary in cloud: STT on meeting audio; fuzzy match on wake phrase. |
| **Audio wake (Vosk / openWakeWord)** | ✅ | Local / external join; disabled on Zoom transcript-only path. |
| **False-wake guards** | ✅ | Device/topic mentions (“my computer”), meeting banter, echo mute, cooldowns. |
| **Directed follow-ups (no wake)** | ✅ Optional | Short window after a reply: “repeat that”, “what about the timeline?” — not open chat. Set `MECHUS_FOLLOWUP_WINDOW_S=0` for wake-only. |
| **Wake opener (“thinking” cue)** | ✅ | Short cached filler while LLM runs; Zoom: no staccato extend fillers (low glitch). |
| **Presence / hear-me** | ✅ | “Computer, can you hear me?” → instant reply. |
| **Stop / dismiss** | ✅ | “Computer, stop” etc.; refined so casual “stop being mad” does not trigger. |
| **Transcript barge-in** | ✅ Optional | `MECHUS_TRANSCRIPT_BARGE_IN` — interrupt TTS when user speaks wake during playback. |
| **VAD / silence gating** | ✅ | `webrtcvad`, configurable silence before speak; tuned for transcript path. |

### Speech-to-text & speakers

| Capability | Status | Notes |
|------------|--------|--------|
| **Continuous meeting transcript** | ✅ | Chunked STT; live lines on status API. |
| **STT backends** | ✅ | faster-whisper (default), OpenAI Whisper, Deepgram, AssemblyAI, ElevenLabs STT. |
| **Zoom per-participant audio** | ✅ | `MECHUS_ZOOM_STREAM_ATTRIBUTION=1` — speaker names from roster. |
| **Pyannote diarization** | ✅ Optional | When mixed audio only; disabled when stream attribution on. |
| **Speaker label overrides** | ✅ | `POST /api/meeting/speakers` → `speaker_maps/*.json`. |
| **Transcript noise filtering** | ✅ | Backchannels, STT tags like `[water running]` ignored for wake. |

### Q&A, memory & grounding

| Capability | Status | Notes |
|------------|--------|--------|
| **Intent routing** | ✅ | Recap, decisions, action items, timeline, who, risks, general, help, stop, presence, etc. |
| **Instant replies** | ✅ | Greeting, help, stop, presence — no LLM. |
| **LLM responses** | ✅ | OpenAI (production); Ollama/rules (dev). |
| **Meeting retrieval (BM25 + optional embeddings)** | ✅ | Light path for low latency on `general` questions. |
| **Context Integrity Engine** | ✅ | Stateful decisions, action items, conflicts, topic namespaces; feeds Q&A. |
| **Conversation memory & dialog anchors** | ✅ | Salient terms, vague follow-up expansion for retrieval. |
| **Response cache** | ✅ | Similar questions reuse short answers (low-latency). |
| **Realtime tools** | ✅ Partial | Weather (OpenWeather), web search (Tavily/Serper), time zones — when query needs external data. |
| **Structured LLM output** | ✅ Optional | JSON mode with `private_thoughts` for dashboard. |
| **Actionable-only mode** | ✅ Optional | `MECHUS_ACTIONABLE_ONLY` — limit when Mechus speaks without explicit asks. |

### Voice output (TTS)

| Capability | Status | Notes |
|------------|--------|--------|
| **ElevenLabs TTS** | ✅ Production | Multiple voices; streaming PCM to Zoom virtual mic. |
| **Piper / local TTS** | ✅ Dev | |
| **Streaming TTS** | ✅ | `MECHUS_ELEVEN_TTS_STREAM=1`; gapless send on Zoom. |
| **Low-latency preset** | ✅ | `MECHUS_LOW_LATENCY`, `MECHUS_CONVERSATIONAL` — lexical retrieval, shorter waits. |
| **Persona-specific voices** | ✅ | Penny, Doc, Codey, Rizzo + default Mechus (`mechus/personas/*.json`). |

### Personas

| Persona | Wake examples | Domain |
|---------|---------------|--------|
| **Default (Mechus)** | `computer`, `mechus`, `hey computer` | General meeting assistant |
| **Penny** | `hey penny`, `penny` | Finance teammate |
| **Doc** | `hey doc`, `doc` | Medical-style boundaries |
| **Codey** | `hey codey`, `codey` | Engineering |
| **Rizzo** | `hey rizzo`, `rizzo` | (configured in JSON) |

Each persona: own ElevenLabs voice, system prompt, filler phrases, refusal rules, optional tools.

### API & dashboard integration

| Endpoint | Purpose |
|----------|---------|
| `GET /health`, `GET /ready` | Liveness / readiness |
| `POST /api/join-meeting` | Start bot for a meeting link + `user_id` |
| `GET /api/status?meeting_id=` | Live status, transcripts snippet, **thoughts[]**, latency, FaceTime state |
| `GET /api/transcript?meeting_id=` | Full transcript + wake cycles while active |
| `POST /api/stop` | End meeting; **final_summary**, email hook |
| `GET /api/personas` | List enabled personas |
| `POST /api/meeting/speakers` | Speaker map CRUD |
| `POST /api/teams`, team context | Team-scoped memory (Supabase-oriented) |
| `GET /share/<meeting_id>` | Share page + card image |
| `POST /api/test-voice` | TTS smoke test |

**Thoughts API** (`thoughts[]` on status): exposes internal reasoning (`thinking`, `context`, `planned`, `connection`) for the dashboard Thoughts column — see `docs/mechus-api-thoughts.md`.

Polling model: dashboard **polls** status/transcript; Mechus does not push webhooks by default.

### Post-meeting & artifacts

| Capability | Status | Notes |
|------------|--------|--------|
| **Session notes (JSONL)** | ✅ | `mechus_notes/<meeting_id>.jsonl` — transcript + wake cycles. |
| **Meeting store / chunks** | ✅ | `chunks.jsonl`, snapshots after responses. |
| **Final summary on stop** | ✅ | LLM executive summary → `final_summary.json` + API response. |
| **Meeting chapters** | ✅ | Auto chapter markers during long meetings. |
| **Email summary** | ⚠️ Implemented | SMTP; requires valid Gmail/app password (often misconfigured). |
| **Share card (PNG)** | ✅ | Social-style meeting card endpoint. |

### Deployment & ops

| Capability | Status | Notes |
|------------|--------|--------|
| **Docker + docker-compose** | ✅ | API on :5001; Zoom override compose file. |
| **`./run.sh deploy`** | ✅ | Rsync to VM + rebuild/restart. |
| **Gunicorn API** | ✅ | Single worker recommended per container for sessions. |
| **FaceTime worker image** | ✅ | Built on deploy; `mechus-facetime-worker:latest`. |
| **Env-driven config** | ✅ | `.env` / `.env.example` — no code change to tune behavior. |
| **API key + CORS** | ✅ | `MECHUS_API_KEY`, `MECHUS_CORS_ORIGINS` for production. |

### Security & compliance (Zoom app)

| Item | Status |
|------|--------|
| SSDLC, security/incident/vuln policies | ✅ Docs in `docs/zoom_beta/` |
| Privacy policy template | ✅ |
| TLS at edge (operator responsibility) | ✅ Documented |
| SAST/DAST evidence | ⚠️ Operator runs scans |

---

## Experimental / Partial Features

These exist in code or docs but are **not** the primary production path:

| Feature | State | Notes |
|---------|--------|--------|
| **Context Event Engine** | Experimental | `mechus/experimental/` — event-based memory, Ollama extraction; parallel to Context Integrity Engine. |
| **RTMS (Zoom Real-Time Media Streams)** | Partial / optional | `docker-compose.rtms.yml`, `mechus/rtms_*` — alternative audio path; not default. |
| **Experimental wake / context metrics** | Dev | Benchmark and simulation scripts under `scripts/`, `evals/`. |
| **GPU / large Whisper models** | Supported via config | Heavier STT; trade latency for accuracy. |
| **Ollama local LLM** | Dev / self-host | `MECHUS_LLM_BACKEND=ollama` |
| **OpenWakeWord** | Optional | `MECHUS_WAKE_BACKEND=openwakeword` |
| **Recording token auto-permission** | Documented, integrate as needed | `docs/PRODUCTION_HEADLESS.md` — reduces host “Allow record” friction. |

---

## Planned Features & Roadmap

From `docs/IMPLEMENTATION_PRIORITY.md`, `docs/SCALABLE_BOT_ARCHITECTURE.md`, and known gaps:

### Near-term (documented priority)

| Priority | Feature | Why |
|----------|---------|-----|
| **1** | Stable Linux bot host (Hetzner-class VM) | Reliable Zoom raw audio; already your deploy target. |
| **2** | **Recording token** in join flow | Remove host “allow recording” popup; backend fetches Zoom jointoken. |
| **3** | **Streaming TTS polish** | Largely done; continue tuning opener→reply handoff and latency metrics. |
| **4** | **VAD / turn detection hardening** | Consistent end-of-utterance before LLM/TTS; reduce talk-over. |
| **5** | **Weather API fix** | Logs show OpenWeather 404 for some queries — endpoint/key/geo resolution. |
| **6** | **Email on stop** | Reliable SMTP or provider (SendGrid/Resend) instead of raw Gmail. |

### Medium-term (architecture)

| Feature | Description |
|---------|-------------|
| **Scalable multi-bot host** | 30–50 dumb Zoom processes per server; STT/LLM/TTS in central “brain” service (`docs/SCALABLE_BOT_ARCHITECTURE.md`). |
| **Session store beyond in-memory** | Redis/DB for multi-instance API without sticky sessions only. |
| **Zoom recording token + join API** | First-class in `POST /api/join-meeting`. |
| **Push/webhooks to dashboard** | Optional替代 polling for status/transcript. |
| **Long-meeting stability** | Address ~20–30 min SDK crash class (documented as common industry issue; root-cause TBD in repo). |

### Longer-term / not started

| Feature | Description |
|---------|-------------|
| **RTMS as default cloud audio** | If SDK path insufficient at scale. |
| **GPU STT farm** | Separate transcription service. |
| **Multi-region orchestration** | Global meeting assignment. |
| **Native mobile FaceTime** | Not possible — no Apple bot API; web-only. |
| **Microsoft Teams / Google Meet SDK bots** | Not in repo; browser join only where applicable. |
| **Penetration test executive summary** | For Zoom Beta (operator-provided). |

---

## Feature Matrix by Join Backend

| Feature | Zoom SDK | FaceTime web | Browser / external |
|---------|----------|--------------|---------------------|
| No browser | ✅ | ❌ (Chromium inside worker) | ❌ |
| Transcript wake | ✅ | ✅ | Optional |
| Audio wake (Vosk) | ❌ (transcript-only) | ❌ | ✅ |
| Per-speaker names | ✅ (stream attribution) | Via STT label | Varies |
| TTS in meeting | ✅ Virtual mic | ✅ PulseAudio | ✅ If devices routed |
| Production deploy | ✅ Primary | ✅ Secondary | Dev |

---

## Key Configuration (Quick Reference)

| Variable | Effect |
|----------|--------|
| `MECHUS_WAKE_PHRASE` | Wake words (comma-separated) |
| `MECHUS_FOLLOWUP_WINDOW_S` | Seconds for no-wake follow-ups (`0` = always require wake) |
| `MECHUS_LOW_LATENCY` / `MECHUS_CONVERSATIONAL` | Faster answers, lighter retrieval |
| `MECHUS_ELEVEN_TTS_STREAM` | Stream TTS to meeting |
| `MECHUS_ZOOM_STREAM_ATTRIBUTION` | Per-participant Zoom audio + names |
| `MECHUS_JOIN_BACKEND` | `zoom_sdk`, `facetime_web`, `browser`, `external` |
| `MECHUS_LLM_BACKEND` | `openai` (prod), `ollama`, `rules` |
| `MECHUS_STT_BACKEND` | `faster-whisper`, `openai`, etc. |

Full list: `.env.example` and `docs/PRODUCTION.md`.

---

## Known Limitations (Honest)

1. **Wake accuracy** — Meeting crosstalk and STT errors can still cause missed or unwanted wakes; tune `MECHUS_WAKE_TRANSCRIPT_MAX_DIST`, follow-up window, and cooldowns.
2. **LLM latency** — External questions (weather + web search) can take 5–10+ seconds; opener plays during wait.
3. **Zoom host friction** — Recording permission may require host action until recording token is wired.
4. **Email** — Often fails with Gmail “BadCredentials” until app passwords configured.
5. **Single API process memory** — Sessions in-process; horizontal scale needs sticky LB or shared store.
6. **PulseAudio on Zoom container** — Warning in logs if PA does not start; audio may still work via SDK path.

---

## Related Documentation

| Doc | Topic |
|-----|--------|
| `README.md` | Quick start, deploy |
| `docs/ARCHITECTURE_MAP.md` | Data flow, modules |
| `docs/IMPLEMENTATION_PRIORITY.md` | Build order |
| `docs/PRODUCTION.md` | Security, scaling |
| `docs/ZOOM_SDK_SETUP.md` | Zoom credentials & join |
| `docs/FACETIME_JOIN.md` | FaceTime worker |
| `docs/mechus-api-thoughts.md` | Thoughts API for dashboard |
| `docs/CONNECT_DASHBOARD.md` | Dashboard integration |
| `docs/CONTEXT_INTEGRITY_ENGINE.md` | Meeting memory model |

---

## Document History

| Date | Change |
|------|--------|
| May 2026 | Initial consolidated features doc (current vs planned). |
