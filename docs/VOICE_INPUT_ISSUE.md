# Why Koov Gets No Voice Input (Meeting SDK, No RTMS)

When you use the **Meeting SDK only** (no RTMS), Koov gets meeting audio through Zoom’s **local recording** subsystem. Four conditions must all be true, in the **correct order**, or you get no audio frames.

---

## 1. Core Constraint — Four Conditions and Pipeline Order

Four conditions must **all** be true before audio frames flow:

1. **Bot joined** (INMEETING).
2. **Recording privilege granted** (host allowed, or join token pre-authorizes).
3. **Raw recording started** (`StartRawRecording()` succeeded).
4. **Raw audio subscription active** (subscribe **after** StartRawRecording; callbacks registered).

**Critical:** If you **subscribe before** `StartRawRecording()`, or if **subscribe** runs before **setExternalAudioSource()**, the SDK can **silently fail** — Zoom drops the subscription or callbacks never fire. The pipeline has **three** distinct steps after privilege is granted:

```text
Join meeting
    ↓
Recording authorized (host allow or token)
    ↓
StartRawRecording()
    ↓
setExternalAudioSource()   ← register virtual mic (output) first
    ↓
subscribe()               ← then subscribe to raw audio (input)
    ↓
Zoom sends frames → callbacks fire
    ↓
STT
```

If any step is skipped or reordered (e.g. subscribe then setExternalAudioSource, or subscribe before StartRawRecording), you get **"No Zoom audio frames after 30s"** even when privilege was granted. Use a **readiness loop** (poll `CanStartRawRecording()` every 1s until SUCCESS) instead of a fixed delay; the recording controller may not be ready exactly N seconds after INMEETING.

**State machine (deterministic flow):**

```text
STATE_JOINING
    ↓
STATE_WAIT_PERMISSION   (readiness loop: CanStartRawRecording every 1s; if NO_PERMISSION → RequestLocalRecordingPrivilege)
    ↓
STATE_START_RECORDING  (StartRawRecording)
    ↓
STATE_SUBSCRIBE_AUDIO  (setExternalAudioSource → subscribe)
    ↓
STATE_RECEIVING_FRAMES (callbacks fire)
```

Implementing this makes the pipeline deterministic and easier to debug.

---

## 2. Why the Host Prompt Often Fails

Assuming "the host will see a prompt" is unreliable. Common causes:

### A. Host disabled participant recording

If **"Allow participants to record"** is off for the meeting, `RequestLocalRecordingPrivilege()` does nothing. No prompt is shown.

### B. Recording disabled at account or meeting level

**Recording must be enabled in the host’s Zoom settings.** If it is disabled at the **account** or **meeting** level, `SDKERR_NO_PERMISSION` persists regardless of the host clicking Allow. Avoid making claims about specific account tiers (e.g. Basic vs Pro) unless you have confirmed them with Zoom; the reliable statement is: **if recording is disabled in the host’s Zoom settings, the SDK will not grant recording to the bot.**

### C. Host joined after the bot

Zoom shows the prompt only if the **host is present when the request is sent**. If the bot joins first, the initial request is lost. **Solution:** Use a readiness loop that keeps calling `RequestLocalRecordingPrivilege()` (e.g. every 1s when `CanStartRawRecording()` returns NO_PERMISSION) so when the host joins they see the next prompt. If the SDK exposes participant/role-change events, re-request when a new participant becomes host.

### D. Waiting room

If the bot hits the waiting room first, the recording request can fail silently.

**Conclusion:** Relying on the host prompt is fragile. Use a **recording join token** for production.

---

## 3. “SUCCESS but No Frames” — Correct Analysis

Logs can show:

```text
subscribe_result=SUCCESS
setExternal_result=SUCCESS
No Zoom audio frames after 30s
```

**Misdiagnoses to avoid:**

- **Silence** — Zoom still sends frames during silence (zeroed samples). If you get **no callbacks at all**, silence is **not** the cause.
- **Sampling mismatch** — Affects **audio quality**, not whether callbacks fire. The SDK resamples internally.

**Real cause (most often):**

**Subscribe before recording actually started**, or **subscribe before setExternalAudioSource()**. Example race:

```text
INMEETING → subscribeRawAudio() → StartRawRecording()
```

Zoom drops the subscription. You must enforce:

```text
StartRawRecording()
    ↓
setExternalAudioSource()
    ↓
subscribeRawAudio()
```

**Do not use a fixed delay.** The recording controller may not be ready exactly 2 seconds after INMEETING (e.g. on slow hosts or large meetings). Use a **readiness loop**:

```text
on INMEETING
  → loop every 1s:
      err = CanStartRawRecording()
      log "CanStartRawRecording returned %s" every time
      if err == SUCCESS → StartRawRecording() → setExternalAudioSource() → subscribe(); exit loop
      if err == NO_PERMISSION → RequestLocalRecordingPrivilege()
      retry in 1s
```

---

## 4. Recording Token — Correct but Incomplete

The recording join token is the right production fix. One thing is often misunderstood:

**The token does not automatically start recording.** It only **authorizes** recording. You still must call **`StartRawRecording()`** after joining (after the short delay). The token lets `CanStartRawRecording()` (or the join) succeed without the host prompt; it does not remove the need to start recording and then subscribe.

**Zoom API:**

```http
GET /v2/meetings/{meetingId}/jointoken/local_recording?bypass_waiting_room=true
Authorization: Bearer {oauth_access_token}
```

Set `JoinParam.app_privilege_token` (and equivalents) to the token, then after INMEETING: wait 1–2 s → `StartRawRecording()` → subscribe.

---

## 5. Required Code Behavior

- **Join:** Set `app_privilege_token` (and sibling attributes) when a recording token is provided.
- **After INMEETING:** Use a **readiness loop**: poll `CanStartRawRecording()` every 1s; when it returns SUCCESS, then **`StartRawRecording()`** → **`setExternalAudioSource()`** → **subscribe()**. Do not use a fixed delay; do not subscribe before setExternalAudioSource.
- **Both callback types:** Implement **both** `onMixedAudioRawDataReceived` and `onOneWayAudioRawDataReceived`. Zoom can send **both** simultaneously. Do not treat one as “primary” and discard the other: **mixed stream** → meeting transcript; **one-way stream** → diarization input. Log which fires first for debugging.
- **Sampling:** Set `param.eAudioRawdataSamplingRate = AudioRawdataSamplingRate_32K` explicitly.
- **When host joins after the bot:** The first recording request may never be delivered. Subscribe to participant/role-change events if the SDK exposes them, and **re-request recording privilege** when a new participant becomes host; otherwise keep re-requesting on a timer (e.g. every 1s when in “wait permission” state) so the host sees the prompt after they join.

---

## 6. Infrastructure — Docker and ALSA (Two Environments)

**Do not assume “Docker + SDK = fragile”.** Many bots run successfully inside Docker. The real issue is **audio device access**. Most **EC2/cloud VMs do not have real sound hardware**, so `/dev/snd` often does not exist. Handle both cases:

**Environment A — VM with real audio device**  
Expose the host’s sound device into the container:

```bash
docker run --device /dev/snd --group-add audio ...
```

**Environment B — Cloud VM (e.g. EC2) with no sound hardware**  
Use an **ALSA loopback** device so the SDK has something to attach to:

```bash
# On the host (once, or at boot):
sudo modprobe snd-aloop
```

If you get **`Module snd-aloop not found`**, the kernel doesn’t ship it by default (common on AWS/Ubuntu server). Install the extra modules for your kernel, then load it:

```bash
sudo apt-get update
sudo apt-get install -y linux-modules-extra-$(uname -r)
sudo reboot
# After reboot:
sudo modprobe snd-aloop
ls -la /dev/snd
```

**If `snd-aloop` still isn’t found after install + reboot:** Some **AWS** kernels (e.g. `6.17.0-1007-aws`) do not include `snd-aloop` in their extra package; sound is often omitted from AWS kernel builds. Options: (1) switch to the **generic** kernel (`sudo apt install linux-generic`, then reboot and select it in GRUB); (2) use a different AMI; (3) run the Zoom SDK on a machine with real audio. Without ALSA (real or loopback), raw-audio callbacks in Docker on that VM will not receive frames.

Then map the loopback device into the container (e.g. `--device /dev/snd` if the host now exposes it). Without a real or loopback ALSA device, raw-audio callbacks can stall.

---

## 7. Stable Architecture — Streaming and Frame Batching

```text
Zoom SDK worker
    ↓
Audio frame stream
    ↓
Batch 20–40 frames (do not push each frame to Redis individually)
    ↓
WebSocket / Redis Stream
    ↓
STT workers
    ↓
Agent
```

The queue should be **streaming** (e.g. WebSocket or Redis Stream), not batch-at-end. **Raw audio arrives at ~100 frames/sec**; pushing every frame individually into Redis will overwhelm the queue. **Collect 20–40 frames, then push one batch** so the pipeline stays stable under load.

---

## 8. What to Fix — Correct Debugging Order

Callbacks cannot fire until permission and subscription exist. Debug in this order:

1. **Verify recording permission** — Recording enabled in host’s Zoom settings; or use recording token. Log `CanStartRawRecording` result **before every attempt** so you can distinguish permission issue vs recording controller not ready.
2. **Verify StartRawRecording succeeded** — Log `raw_recording_started` after `StartRawRecording()` returns SUCCESS.
3. **Verify subscription and order** — `setExternalAudioSource()` then `subscribe()`; log both results.
4. **Verify callbacks** — Log when first frame arrives and which stream (mixed vs one-way).
5. **Implement recording token** — Backend fetches token, passes to join; use readiness loop then pipeline above.
6. **Improve infrastructure** — ALSA: real device or `snd-aloop` on cloud VM; expose to container.

---

## 9. Logging — So You Know Where the Pipeline Breaks

Log at least:

- **CanStartRawRecording result** — **Before every attempt** (in the readiness loop). Without this you cannot distinguish *permission denied* vs *recording controller not initialized yet*.
- **raw_recording_started** — After `StartRawRecording()` returns SUCCESS.
- **setExternalAudioSource result** — Before subscribe.
- **subscribe_raw_audio_result** — Return value of subscribe (success/fail).
- **First frame** — When the first `onMixedAudioRawDataReceived` or `onOneWayAudioRawDataReceived` fires, and **which** stream (mixed vs one-way).

Without these, you cannot tell at which step the pipeline failed.

---

## 10. Container Configuration Example

**If the host has sound hardware** (`/dev/snd` exists):

```bash
docker run --device /dev/snd --group-add audio -e ... your-koov-zoom-image
```

**If the host is a cloud VM with no sound device**, load the ALSA loopback module on the host first, then expose the device:

```bash
# On host (once or at boot):
sudo modprobe snd-aloop
# Then run container with --device /dev/snd (if the host now exposes it) or map the loopback device.
```

In Docker Compose:

```yaml
devices:
  - /dev/snd
group_add:
  - audio
```

(Only valid when the host has `/dev/snd`; on typical EC2 instances use `snd-aloop` as in §6.)

---

## 11. Summary Table

| Situation | Cause | What to do |
|-----------|--------|------------|
| **SDKERR_NO_PERMISSION** | Privilege not granted: Basic account, recording off, host joined later, or waiting room. | Host must have account that can grant recording; or use **recording token**. |
| **Subscribe SUCCESS but no frames** | Subscribe before StartRawRecording, or subscribe before setExternalAudioSource. | Enforce: StartRawRecording() → setExternalAudioSource() → subscribe(). Use readiness loop, not fixed delay. Log CanStartRawRecording every attempt. |
| **Want reliable voice input** | Host prompt and account limits are unreliable. | **Recording join token** + correct order + ALSA in container. |
| **Host is Basic (free) account** | Basic cannot grant recording to SDK; token may not work for Basic‑owned meetings. | See **§13** (meeting created by Pro, or RTMS). |

---

## 12. Pipeline Diagram (Reference)

```text
Join meeting
    ↓
Recording authorized (token or host allow; readiness loop until CanStartRawRecording == SUCCESS)
    ↓
StartRawRecording()
    ↓
setExternalAudioSource()
    ↓
subscribe()
    ↓
Receive frames (onMixed… / onOneWay…)
    ↓
STT → Agent
```

For OAuth, token fetch, and join code: **PRODUCTION_HEADLESS.md**, **scripts/fetch_zoom_recording_token.py**, **KOOV_HEAR_ME.md**.

---

## 13. Without a Pro Account (Basic / Free Host)

If the **meeting host** is on a Zoom **Basic (free)** account:

- **Host prompt path does not work.** Basic accounts cannot grant local recording to SDK participants, so you get `SDKERR_NO_PERMISSION` no matter how many times the host clicks Allow.
- **Recording token** is obtained via Zoom’s API by a user who has permission for that meeting (usually the meeting **owner** or an admin). Zoom often ties recording and the jointoken API to accounts that have **recording enabled** (typically Pro or higher). If the meeting is **scheduled by a Basic account**, the API may not return a token or the token may not grant recording when the bot joins.

**Practical options without the host having Pro:**

1. **Meeting created by a Pro (or recording‑capable) account**  
   Have meetings that Koov joins **scheduled or started by a Zoom account that has recording** (e.g. Pro). That account (or an OAuth app acting on its behalf) calls `GET /meetings/{meetingId}/jointoken/local_recording` and passes the token to Koov. **Basic users join as participants**; they don’t need Pro. Only the account that “owns” the meeting and gets the token needs to support recording.  
   Example: one “Koov” or “team” Pro account creates the meeting and your backend uses that account’s OAuth to fetch the recording token; invitees can be Basic.

2. **Reconsider RTMS (Zoom Apps)**  
   If you’re open to it, **Real-Time Media Streams (RTMS)** use a different permission model: the **user** joins the meeting **with your Zoom App**. Zoom then streams audio to your backend. This can work when the host is Basic, because the user is authorizing your app, not the host granting “participant recording.” You said you don’t want RTMS; this is the main Zoom-first alternative if you need to support Basic hosts. See **RTMS_SETUP.md** and **ZOOM_APP_KOOVAPI_SETUP.md**.

3. **Single Pro account for Koov meetings**  
   Use one Pro account only for creating/linking meetings that Koov joins. All other participants can stay on Basic. Your backend uses that Pro account’s OAuth to get the recording token and to join the bot. No need for every host to have Pro.

**Summary:** Without any Pro account, the Meeting SDK raw-audio path (host prompt or recording token for a Basic‑hosted meeting) is unreliable or blocked. To support Basic hosts and still use the SDK, use a **Pro (or recording‑capable) account only for the meeting owner/token**, or use **RTMS** if you’re willing to switch to the in-client app flow.
