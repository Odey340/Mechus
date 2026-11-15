# Implementation Priority: What to Build First

This doc captures the correct order of work: what’s solid, what’s weak, and what to implement first. Avoid premature work on multi-server scaling, RTMS, or GPU STT until you have **one reliable bot**, **low latency**, and **no meeting friction**.

---

## What’s solid vs weak in the plan

### 1. Running Mechus on a real Linux host — **Correct**

Running the Zoom Meeting SDK bot on real Linux is the right move.

- Zoom raw audio callbacks depend on stable audio timing.  
- Mac Docker adds virtualization jitter.  
- Zoom SDK is tested primarily on Linux production servers.

**Recommendation:** Spin up **Hetzner CPX31** (4 vCPU, 8 GB RAM, Ubuntu 22.04), ~$14/month. Cheap and stable.

---

### 2. Recording token — **Good, but not the whole story**

**Flow:**

```
Backend
  → Zoom API: GET /meetings/{id}/jointoken/local_recording
  → recording_token
  → POST /api/join-meeting (with recording_token)
  → Mechus bot joins with recording enabled
```

**Advantages:** No host popup, automatic recording permission, fully first-party Zoom.

**Limitation:** Recording tokens **only solve permission**. They do **not** improve audio reliability or latency. The real audio pipeline still depends on Zoom Meeting SDK raw audio callbacks. So this step is **necessary but not sufficient** — do it, but don’t expect it to fix audio issues; those are fixed by running on real Linux and a solid audio path.

---

### 3. Streaming TTS (ElevenLabs) — **Critical**

This is more important than it sounds.

**Bad (common mistake):**

```
LLM → full response → TTS → full WAV → send to Zoom
Latency: 2–4 seconds
```

**Good:**

```
LLM partial output → ElevenLabs streaming TTS → audio chunks → send to Zoom immediately
Latency: ~600–900 ms
```

That gap is the difference between a robot that interrupts meetings and a bot that feels conversational. Implement streaming TTS and send PCM chunks to Zoom as they arrive.

---

### 4. VAD / turn detection — **The missing gate**

The agent must detect **“speaker finished talking”**. Otherwise Mechus will:

- Talk over people  
- Answer too late  
- Trigger awkward pauses  

**Minimal logic:**

```
audio input → VAD detects silence > 400 ms → send transcript to LLM → generate response
```

Without this gate, even perfect STT/LLM/TTS will behave badly.

**Current code:** The codebase already has `webrtcvad`, `SilenceMonitor`, and `_wait_for_silence()` with configurable `min_silence_before_speaking_s` (default 0.6 s) and `end_silence_seconds`. Step 4 is to **harden and tune** this (e.g. ensure 400 ms silence threshold is used where appropriate, and that response gating is applied consistently before sending to LLM and before playing TTS).

---

## Ordered next steps (do in this order)

| Step | What | Why |
|------|------|-----|
| **1** | **Linux bot server** | Ubuntu 22.04 on Hetzner CPX31. Run the Zoom bot there. Stable audio callbacks. |
| **2** | **Recording token** | Backend calls `GET /meetings/{id}/jointoken/local_recording`, pass token to bot. Removes host friction. |
| **3** | **Streaming TTS** | Switch ElevenLabs to streaming; send PCM chunks to Zoom as they arrive. Cuts latency to ~600–900 ms. |
| **4** | **VAD / turn detection** | Speech detection, silence threshold (e.g. 400 ms), response gating. Bot stops talking over people and feels intelligent. |

---

## What not to worry about yet

Avoid premature optimization on:

- Multi-server scaling (orchestrator, host agent, dumb bot)  
- RTMS  
- GPU STT  

Right now you need: **one reliable bot**, **low latency responses**, **no meeting friction**. Then scale.

---

## Near-term architecture (production-ready foundation)

```
Zoom Meeting
      ↓
Mechus Bot (Linux VM)
      ↓
Audio stream
      ↓
Streaming STT
      ↓
Mechus decision engine
      ↓
OpenAI LLM
      ↓
ElevenLabs streaming TTS
      ↓
PCM chunks
      ↓
Zoom SDK audio sender
```

This is the clean, production-ready foundation. After this is stable, add the scalable multi-bot design from [SCALABLE_BOT_ARCHITECTURE.md](SCALABLE_BOT_ARCHITECTURE.md).

---

## Common failure: bots crashing after 20–30 minutes

Many meeting-AI startups see Zoom bots **randomly crash after ~20–30 minutes** of meetings. The cause is extremely common and fixable once identified. When you have the root cause (e.g. Zoom SDK lifecycle, memory, heartbeat, or timeout), document it here and add a short “Avoid this” section so future deployments don’t repeat the mistake.

*(Placeholder: add the single biggest mistake and fix once known.)*

---

## See also

- [SCALABLE_BOT_ARCHITECTURE.md](SCALABLE_BOT_ARCHITECTURE.md) — for when you’re ready to scale to 30–50 bots per server.  
- [PRODUCTION_HEADLESS.md](PRODUCTION_HEADLESS.md) — recording token flow and Zoom API details.  
- [CAPACITY_AND_COST.md](CAPACITY_AND_COST.md) — server sizing and cost per meeting.
