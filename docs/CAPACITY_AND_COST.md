# Capacity and Cost per Meeting

The right metric is **cost per simultaneous meeting**, not "cheapest server." A low-price machine that runs only 1–2 bots costs more per meeting than a slightly more expensive machine that runs 8.

---

## What determines how many meetings one server can run

A Koov bot server uses resources for:

1. Zoom SDK bot process  
2. Audio buffering and streaming  
3. STT (often off-server via API)  
4. LLM requests (off-server)  
5. TTS (often off-server via API)  

The heavy compute (STT, LLM, TTS) usually runs off-server via APIs, so the **server mainly handles**:

- Zoom SDK (join, raw audio, virtual mic)  
- Audio buffers and networking  
- Orchestration (one process/container per meeting)  

So one server can realistically handle **multiple meetings**.

**Typical resource usage per meeting bot:**

| Resource | Per meeting (estimate) |
|----------|------------------------|
| CPU      | 0.3–0.6 vCPU          |
| RAM      | 300–600 MB             |

**Example:** A **4 vCPU / 8 GB RAM** server can handle roughly **6–10 meetings** if coded efficiently (one process or container per meeting).

---

## Current Koov AI stack (variable costs)

The **server** runs Zoom SDK + audio buffering. The **variable cost per meeting** comes from external APIs:

| Component | Current provider | Note |
|-----------|------------------|------|
| **STT**   | Cheap option     | Low-cost speech-to-text (e.g. Vosk self-hosted, or a low-cost API). |
| **LLM**   | OpenAI           | Used for reasoning and grounded answers. |
| **TTS**   | ElevenLabs       | Used for voice output into the meeting. |

So total cost per meeting = **server share** (from table below) + **STT + OpenAI + ElevenLabs** usage. The server cost is fixed per box; the API costs scale with meeting duration and how often the agent speaks.

---

## Cost per meeting: provider comparison

### 1. Hetzner (best cost/performance)

Widely used by startups for good price/performance.

| Server  | Specs           | Price       | Est. meetings | Cost per meeting |
|---------|-----------------|------------|---------------|-------------------|
| **CPX31** | 4 vCPU, 8 GB RAM, 160 GB SSD | ~€13/mo (~$14) | 6–10 | **~$1.50–2.30/mo** |
| **CPX41** | 8 vCPU, 16 GB RAM           | ~€26/mo (~$28) | 15–20 | **~$1.40–1.85/mo** |

AWS cannot compete on raw cost at this level.

---

### 2. DigitalOcean (very easy)

| Server     | Specs           | Price     | Est. meetings | Cost per meeting |
|------------|-----------------|----------|---------------|-------------------|
| 4 vCPU / 8 GB | 4 vCPU, 8 GB RAM | $48/mo   | 6–10          | **~$5–8/mo**      |

Same ballpark capacity as Hetzner CPX31, higher price.

---

### 3. AWS EC2 (most expensive)

| Instance   | Specs           | Price (on-demand) | Est. meetings | Cost per meeting |
|------------|-----------------|-------------------|---------------|-------------------|
| t3.xlarge  | 4 vCPU, 16 GB   | ~$120/mo         | 6–10          | **~$12–20/mo**    |

You pay more for ecosystem and enterprise features; same workload as a much cheaper Hetzner/DO box.

---

## Architecture: multiple bots per server

**Avoid:** 1 server = 1 meeting bot (wasteful).

**Do:** One server runs **multiple** bot processes or containers:

```
Server (e.g. 4 vCPU, 8 GB)
 ├ Bot process / container 1  → Meeting A
 ├ Bot process / container 2  → Meeting B
 ├ Bot process / container 3  → Meeting C
 ├ ...
 └ Bot process / container N  → Meeting N
```

Each meeting gets its own process or container; the API (or orchestrator) routes join/status/stop to the right session (e.g. by `meeting_id`). Sticky sessions at the load balancer ensure all requests for a given meeting hit the same instance. See [PRODUCTION.md](PRODUCTION.md) and [MULTIPLE_MEETINGS_SETUP.md](MULTIPLE_MEETINGS_SETUP.md).

**Example for 4 vCPU / 8 GB (e.g. Hetzner CPX31):**

- Meeting bots: **8**
- Server cost: **~$14/month**
- **Cost per meeting: ~$1.75/month**

Your real variable costs will be **STT, TTS, and LLM** usage, not the server.

---

## Even higher capacity: separate bot from AI pipeline

To push **15+ meetings** on a similar-sized server, separate:

- **Bot server:** Zoom SDK + audio streaming only (join, receive PCM, send TTS bytes).
- **AI pipeline:** STT, LLM, TTS run elsewhere (e.g. serverless, separate service, or APIs).

```
Zoom Meeting
      ↓
Koov Bot Server (Zoom SDK, audio buffers)
      ↓
Audio stream
      ↓
AI processing (STT → LLM → TTS, e.g. serverless or API)
      ↓
TTS audio
      ↓
Back to bot → Zoom (virtual mic)
```

Then one server is mostly I/O and Zoom SDK; CPU/RAM per meeting drop, so you can run more bots per box.

---

## Recommended starting point for Koov

| Phase   | Server       | Specs           | Cost     | Capacity   |
|---------|--------------|-----------------|----------|------------|
| **Start** | Hetzner CPX31 | 4 vCPU, 8 GB   | ~$14/mo  | **8 meetings** |
| **Scale** | Hetzner CPX41 | 8 vCPU, 16 GB  | ~$28/mo  | **15–20 meetings** |

**Target architecture:**

```
Zoom Meeting
     ↓
Bot (container/process)
     ↓
Audio → STT service
     ↓
LLM reasoning
     ↓
TTS
     ↓
Bot sends voice back to Zoom
```

Multiple bot containers/processes run on **one** server; load balancer uses sticky sessions so each meeting stays on the same instance.

---

## Relation to other docs

- **30–50 bots per server (dumb bot + scheduler + host agent):** [SCALABLE_BOT_ARCHITECTURE.md](SCALABLE_BOT_ARCHITECTURE.md) — required reading if you want high concurrency without burning money or random bot deaths.  
- **Provisioning and security:** [PRODUCTION.md](PRODUCTION.md)  
- **Multiple meetings in one API:** [MULTIPLE_MEETINGS_SETUP.md](MULTIPLE_MEETINGS_SETUP.md)  
- **Zoom SDK on Linux:** [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md), [AUDIO_WITHOUT_RTMS.md](AUDIO_WITHOUT_RTMS.md)  
- **Low-cost but low-capacity options (e.g. 1–2 meetings):** [CHEAPEST_DEPLOYMENT.md](CHEAPEST_DEPLOYMENT.md) — use when you only need a single bot; for multiple meetings, cost per meeting is better with the sizing above.
