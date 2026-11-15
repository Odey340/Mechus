# FaceTime Camera and Visual Understanding Implementation Plan

Status: implemented behind default-off flags; pending Phase 0 validation on the Linux VM (see FACETIME_CAMERA_RUNBOOK.md)  
Scope: FaceTime web workers only  
Primary safety rule: camera and vision failures must never prevent or degrade the existing audio-only bot

## 1. Outcome

Add two separately controlled capabilities to each FaceTime worker:

1. **Outbound virtual camera** — FaceTime participants can see a Koov avatar, activity animation, or an approved status card.
2. **On-demand visual understanding** — when explicitly requested, Koov captures one rendered FaceTime frame, analyzes the relevant visible content, answers the question, and discards the frame.

The first production release must not continuously record video, perform face recognition, infer emotion or sensitive traits, expose private reasoning, or attempt browser screen sharing.

Apple's FaceTime web client supports camera access and camera on/off controls, but it does not support screen sharing. Therefore screen sharing is not part of this project: <https://support.apple.com/en-us/109364>.

## 2. Definition of done

The project is complete only when all of the following are true on the deployment VM and in a real FaceTime call:

- A camera-enabled worker appears in Chromium as a uniquely assigned `Koov Virtual Camera`.
- The host can admit Koov and a second physical Apple device can see Koov's video.
- Koov's existing virtual microphone, incoming audio, transcription, wake handling, TTS, and meeting shutdown still work.
- Camera enable/disable and scene changes are reflected remotely within five seconds.
- Two simultaneous workers use different video devices and never receive each other's frames or commands.
- Missing devices, exhausted capacity, producer crashes, selector failures, and vision failures fall back to audio-only operation.
- Camera and vision are disabled by default until explicitly enabled by configuration or the join request.
- Vision captures are made only for an authorized request and are not retained by default.
- All unit, integration, regression, deployment, and real-call acceptance gates in this document pass.

## 3. Current implementation

### 3.1 Existing pieces to preserve

- `koov/api_server.py` starts one Docker worker per FaceTime bot and monitors it using Docker plus `worker-status-<meeting_id>.json`.
- The API and worker share the notes directory, which already provides a durable control/status boundary.
- `koov/facetime_worker.py` drives the FaceTime web client using Selenium and headed Chromium in Xvfb.
- The worker already grants `audioCapture` and `videoCapture` permission to `https://facetime.apple.com` before navigation.
- PulseAudio creates separate call-listening and Koov-TTS paths. Chromium's microphone is deliberately routed to `koov_mic_source`.
- The worker already takes diagnostic Selenium screenshots when the FaceTime UI cannot be controlled.
- The worker already reports incremental state through `_facetime_status_payload`.
- Pillow and NumPy are already application dependencies and can render video frames.

### 3.2 Missing pieces

- No `/dev/video*` device is passed into worker containers.
- No process produces camera frames.
- No FaceTime camera selector or camera-toggle controller exists.
- The worker requests `getUserMedia({audio: true, video: false})` only.
- The worker image does not contain FFmpeg or V4L2 diagnostic utilities.
- There is no API/worker protocol for live camera commands.
- There is no visual-query intent or image-analysis module.
- Status does not expose camera or vision health.

### 3.3 Constraint: do not use Chromium's global fake-media switch

Do not add `--use-fake-device-for-media-stream` to the production worker. Chromium documents that this replaces the actual camera **and microphone**, which would bypass the current PulseAudio microphone design: <https://chromium.googlesource.com/chromium/src/+/HEAD/media/base/media_switches.cc>.

A Y4M fake-camera file may be used only in an isolated experiment. It is not the production design because dynamic scenes are awkward and the required fake-device path risks replacing Koov's microphone.

## 4. Architectural decisions

### 4.1 Virtual camera technology

Use the Linux `v4l2loopback` kernel module on the deployment host. It exposes virtual V4L2 devices that Chromium treats like webcams. Configure `exclusive_caps=1`, which the project recommends for Chrome/WebRTC, and create one device per possible concurrent FaceTime worker: <https://github.com/v4l2loopback/v4l2loopback/blob/main/README.md>.

The module belongs on the host, not in the worker image. Workers receive a single device using Docker's `--device` option.

### 4.2 Frame production

Render frames in Python using Pillow and NumPy. Feed RGB frames to a supervised FFmpeg subprocess, which converts them to the exact pixel format negotiated during the host spike and writes them to the assigned V4L2 device.

Start with:

- Resolution: 640x360; fall back to 640x480 if FaceTime or Chromium negotiates only 4:3 modes.
- Frame rate: 12 FPS; configurable between 5 and 20.
- Pixel format: determine with `v4l2-ctl --list-formats-ext`; prefer a Chromium-compatible uncompressed format such as YUYV.
- Frame policy: always render the newest state and drop stale frames rather than buffering them.

If FFmpeg cannot produce a stable Chrome-readable stream during Phase 0, test GStreamer's `v4l2sink`. Do not support both pipelines in production unless the spike proves that platform variation requires it.

### 4.3 Control protocol

Use two different protocols because camera state and visual questions have different semantics:

1. **Camera desired state** is an atomically replaced JSON file. Latest state wins, repeated processing is harmless, and a restarted worker can restore the intended scene.
2. **Visual questions** are one-shot commands in a spool directory with unique request IDs and explicit result files.

Do not use a shared append-only JSONL file for control messages; concurrent or interrupted writes can create partial commands and make acknowledgement ambiguous.

### 4.4 Failure policy

`camera_required` defaults to false. If the device pool is exhausted or camera startup fails, the worker joins audio-only and reports a warning. Only an explicit `camera_required: true` request may fail before starting the worker.

Vision never controls whether the worker joins or remains in the meeting.

## 5. Runtime architecture

```text
Linux VM
├── v4l2loopback device pool
│   ├── /dev/video20  Koov Camera 1
│   ├── /dev/video21  Koov Camera 2
│   └── ...
│
├── Koov API container
│   ├── camera device lease manager
│   ├── POST camera desired state
│   ├── POST visual query
│   └── GET status/result
│
└── FaceTime worker container
    ├── /dev/video20 passed with Docker --device
    ├── Python scene renderer
    ├── FFmpeg -> /dev/video20
    ├── Chromium -> FaceTime outgoing video
    ├── Selenium camera controller
    ├── existing PulseAudio audio graph, unchanged
    └── optional screenshot -> vision request -> Koov answer
```

## 6. Host provisioning

### 6.1 New scripts

Add `scripts/setup_facetime_cameras.sh`:

- Linux-only and explicitly requires root/sudo.
- Installs or verifies `v4l2loopback-dkms`, matching kernel headers, `v4l-utils`, and module-loading support.
- Refuses to make changes if the detected kernel headers do not match the running kernel.
- Creates a persistent module configuration with a configurable device count, explicit video numbers, card labels, and `exclusive_caps=1`.
- Documents Secure Boot module-signing requirements instead of disabling Secure Boot.
- Loads the module and verifies all expected device nodes.
- Is idempotent.

Add `scripts/verify_facetime_camera_host.sh`:

- Read-only.
- Confirms Linux, module loaded, expected devices present, permissions, `exclusive_caps`, supported formats, and Docker availability.
- Produces a machine-readable nonzero exit code and concise remediation text.
- Never installs packages or loads modules.

### 6.2 Deployment integration

Update `scripts/restart-on-server.sh`:

- Run the read-only verifier before building when `KOOV_FT_CAMERA_FEATURE=1`.
- Fail deployment if the camera feature is required and host prerequisites are missing.
- Warn and continue when camera is globally disabled.
- Build the FaceTime image only after preflight succeeds.
- After startup, run a container-level device enumeration probe when camera is enabled.

Update `docker-compose.zoom.yml` so the API receives pool configuration. The API container itself does not require the video devices; the host Docker daemon resolves `--device` for sibling workers.

### 6.3 Host configuration

Add these settings to `.env.example`:

```dotenv
KOOV_FT_CAMERA_FEATURE=0
KOOV_FT_CAMERA_REQUIRED=0
KOOV_FT_VIDEO_DEVICES=/dev/video20,/dev/video21,/dev/video22,/dev/video23
KOOV_FT_VIDEO_WIDTH=640
KOOV_FT_VIDEO_HEIGHT=360
KOOV_FT_VIDEO_FPS=12
KOOV_FT_CAMERA_AUTO_ON=0
KOOV_FT_CAMERA_DEFAULT_MODE=avatar
KOOV_FT_VISION_ENABLED=0
KOOV_OPENAI_VISION_MODEL=
KOOV_FT_VISION_MAX_REQUESTS_PER_MINUTE=3
KOOV_FT_VISION_RETAIN_SECONDS=0
```

## 7. Device lease manager

Add `koov/facetime_camera_lease.py` with a small, independently testable API:

```python
@dataclass(frozen=True)
class CameraLease:
    device: str
    meeting_id: str
    container_name: str
    acquired_at: float

class CameraLeaseManager:
    def acquire(self, meeting_id: str, container_name: str) -> CameraLease | None: ...
    def release(self, meeting_id: str) -> None: ...
    def reconcile(self, running_workers: list[WorkerCameraClaim]) -> None: ...
    def snapshot(self) -> list[CameraLease]: ...
```

Implementation requirements:

- Validate every configured value against the exact pattern `/dev/video<digits>`.
- Never accept a device path from a public API request.
- Store lease metadata under the API notes directory.
- Protect read-modify-write operations with `fcntl.flock`.
- Write metadata to a temporary file, `fsync`, then use `os.replace`.
- Treat Docker container labels as the recovery source of truth after an API restart.
- Add worker labels for meeting ID and video device.
- Reconcile leases during pre-join orphan cleanup and when worker containers exit.
- Release a lease if `docker run` fails.
- Do not release a device merely because the in-memory session table is temporarily missing; verify the labeled container is absent or stopped.

Refactor the duplicate single-bot and multi-bot Docker command construction into a shared helper. The helper must add:

```text
--device /dev/video20:/dev/video20:rwm
--label koov.facetime=1
--label koov.meeting_id=<safe meeting id>
--label koov.video_device=/dev/video20
-e KOOV_FT_VIDEO_DEVICE=/dev/video20
```

The helper must preserve current env-file precedence, notes mount, names, cleanup behavior, and multi-bot persona overrides.

## 8. Worker image and entrypoint

Update `docker/facetime-worker/Dockerfile`:

- Install FFmpeg and `v4l-utils` without recommended extras.
- Do not install kernel modules or kernel headers in the image.
- Add an image-build smoke check such as `ffmpeg -version` and `v4l2-ctl --version`.

Update `docker/facetime-worker/entrypoint.sh`:

- When `KOOV_FT_VIDEO_DEVICE` is present, verify that it is a character device and readable/writable.
- Do not start a separate unsupervised shell-background producer.
- Let the Python worker own the producer lifecycle so shutdown, restart, and health reporting are deterministic.
- Keep PulseAudio and Xvfb startup ordering unchanged.
- A camera validation failure should be exported/logged and left for the worker to downgrade to audio-only unless camera is required.

## 9. Video renderer

Add `koov/facetime_video.py`.

### 9.1 Public interface

```python
class CameraMode(str, Enum):
    OFF = "off"
    AVATAR = "avatar"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    STATUS_CARD = "status_card"
    MESSAGE_CARD = "message_card"

@dataclass(frozen=True)
class VideoScene:
    enabled: bool
    mode: CameraMode
    display_name: str
    text: str = ""
    expires_at: float | None = None

class VirtualCameraOutput:
    def start(self) -> CameraHealth: ...
    def set_scene(self, scene: VideoScene) -> None: ...
    def set_audio_level(self, rms: float) -> None: ...
    def health(self) -> CameraHealth: ...
    def stop(self, timeout_s: float = 5.0) -> None: ...
```

### 9.2 Renderer requirements

- Render a branded but neutral default avatar without requiring network access.
- Keep text within safe margins and wrap by measured pixel width.
- Reject or truncate message-card text above 200 characters.
- Never accept HTML, file paths, URLs to remote images, or arbitrary font paths from an API command.
- Use a bundled/openly licensed font or a known system font with deterministic fallback.
- Use monotonic time for animation and expiry decisions.
- Render to a latest-frame buffer; the writer thread consumes only the newest frame.
- Clamp audio level input and smooth it before rendering a waveform.
- Catch renderer exceptions and emit a safe fallback frame.

### 9.3 Producer supervision

- Start FFmpeg with a direct argv list, never `shell=True`.
- Capture and cap stderr so a noisy process cannot exhaust memory.
- Detect broken pipe, early exit, and stale frame timestamps.
- Restart at most three times with bounded backoff.
- After repeated failure, mark the camera unavailable and leave the FaceTime audio session running.
- Terminate, wait, then kill only the owned child process during shutdown.
- Do not leave the V4L2 device open after worker exit.

## 10. FaceTime camera controller

Add `koov/facetime_camera.py` and keep Selenium-specific behavior out of the renderer.

### 10.1 Responsibilities

- Call `navigator.mediaDevices.enumerateDevices()` after permission grant.
- Confirm exactly one expected Koov `videoinput` is visible.
- Record labels and counts without persisting opaque device IDs.
- Open the FaceTime camera menu using accessibility labels and roles.
- Select the option containing the configured camera label.
- Toggle the camera using the button's accessible name and `aria-pressed`/state attributes.
- Verify the result using at least two signals:
  - camera button state;
  - a live local video track or local preview when exposed by the page.
- Recheck after admission and periodically during the call.
- Save a diagnostic screenshot and sanitized DOM metadata on failure.

### 10.2 Selector strategy

- Prefer semantic roles and accessible labels over generated CSS class names.
- Search normal DOM and open shadow roots where available.
- Keep all camera-label synonyms in one tested constant.
- Reuse/refactor the existing microphone-menu event dispatch helper rather than copying large JavaScript blocks.
- Never click a generic menu item unless its normalized label matches the Koov camera.
- Apply bounded retries; camera repair must not stall the main worker state loop.

### 10.3 Startup order

1. Acquire/pass device before container start.
2. Start `VirtualCameraOutput` before Chromium so `exclusive_caps=1` exposes a capture-capable device.
3. Launch Chromium with the current PulseAudio options unchanged.
4. Grant audio and video permissions.
5. Navigate and join with camera off unless explicitly configured otherwise.
6. After `facetime_state == in_call`, select the camera.
7. Turn it on only when desired state says enabled.
8. Report selection and track health in worker status.

Do not change the existing audio-only `getUserMedia` keepalive call to request video. FaceTime should own its outgoing video track after selecting the real V4L2 camera; Koov's probe may request a short, separately stopped video stream only when diagnosing enumeration.

## 11. Activity state integration

Do not have the renderer reach into many private `KoovAgent` fields.

Add a small read-only method to `KoovAgent`:

```python
def get_runtime_activity(self) -> dict[str, object]:
    return {
        "tts_playing": bool(...),
        "wake_cycle_active": bool(...),
        "stopping": bool(...),
    }
```

The worker maps activity to scenes only when the current desired mode is `activity`/automatic:

- TTS playing -> `speaking`.
- Wake cycle active before TTS -> `thinking`.
- In call and neither -> `listening`.
- Joining/waiting -> `avatar`.

For the speaking waveform, prefer an outgoing-audio RMS hook if it can be added without changing timing. If that hook creates audio risk, ship a deterministic speaking animation first and add audio-reactive motion later.

## 12. Camera desired-state protocol

Add an atomic control file:

```text
/notes/camera-control-<safe_meeting_id>.json
```

Schema:

```json
{
  "schema_version": 1,
  "revision": 4,
  "meeting_id": "meeting-123",
  "enabled": true,
  "mode": "status_card",
  "text": "Koov is preparing the summary",
  "expires_at": 1794680060.0,
  "requested_at": 1794680000.0
}
```

Rules:

- API assigns monotonically increasing revisions under a file lock.
- Worker applies only revisions newer than its last acknowledged revision.
- The meeting ID inside the file must match the worker's own ID.
- Allowed modes are an enum; unknown fields do not alter runtime behavior.
- Text maximum is 200 characters after Unicode normalization.
- Duration range is 1–60 seconds.
- `expires_at` is transported as wall-clock epoch time; after validation, the worker converts the remaining duration to a monotonic deadline so system-clock adjustments cannot extend a displayed card.
- On expiry, transient cards return to the previous persistent mode.
- Worker status contains desired and actual state plus the acknowledged revision.
- Stop cleanup may retain the last desired-state file for diagnostics, but it must not be reused by a future meeting with a different ID.

## 13. API changes

### 13.1 Join request

Extend `POST /api/join-meeting` with:

```json
{
  "camera": {
    "enabled": false,
    "required": false,
    "mode": "avatar"
  },
  "vision": {
    "enabled": false
  }
}
```

Validation:

- Ignore camera/vision settings for non-FaceTime backends with a response warning rather than changing Zoom behavior.
- Reject malformed object types and unknown modes with HTTP 400.
- A request may enable a globally available feature but may not override a global security disable.
- Never accept a device path, FFmpeg arguments, image path, model endpoint, or retention path from the client.

### 13.2 Camera endpoint

Add:

```http
POST /api/meetings/<meeting_id>/camera
```

Accepted actions:

```json
{"action":"turn_on"}
{"action":"turn_off"}
{"action":"set_mode","mode":"avatar"}
{"action":"show_card","mode":"status_card","text":"Koov is summarizing","duration_seconds":10}
```

Return the accepted desired state and revision. Return:

- 202 while the worker has not yet acknowledged the revision.
- 200 if the worker already reports the requested actual state.
- 404 for unknown meetings.
- 409 when the meeting exists but is not a FaceTime worker.
- 503 when camera support is globally disabled.

The existing global API-key middleware protects this endpoint when `KOOV_API_KEY` is configured. Production deployment must treat an empty API key as a failed readiness condition when camera/vision is enabled.

### 13.3 Status fields

Add these to both the live-session and disk-reconstructed `/api/status` paths:

```json
{
  "facetime_camera_feature_enabled": true,
  "facetime_camera_available": true,
  "facetime_camera_device_assigned": true,
  "facetime_camera_producer_ok": true,
  "facetime_camera_selected": true,
  "facetime_camera_enabled": true,
  "facetime_camera_desired_mode": "activity",
  "facetime_camera_actual_mode": "speaking",
  "facetime_camera_control_revision": 4,
  "facetime_camera_last_frame_at": 1794680000.0,
  "facetime_camera_error_code": "",
  "facetime_camera_error": "",
  "facetime_vision_enabled": false,
  "facetime_vision_busy": false
}
```

Do not expose host device paths, opaque browser device IDs, full screenshots, model prompts, or captured image data through status.

### 13.4 Multi-bot behavior

- Each bot receives its own lease and camera desired-state file.
- The group status response includes a compact camera object per bot.
- A future group camera endpoint may fan out commands, but the first release controls each `bot_meeting_id` explicitly to avoid accidental broadcast.
- If a multi-bot start partially fails, release leases for workers that never started and report per-bot warnings.

## 14. Visual understanding

### 14.1 Scope

Visual understanding is on-demand image analysis, not continuous video analysis. OpenAI's supported image-input flow can analyze images and visible text: <https://developers.openai.com/api/docs/guides/images-vision>.

Supported first-release questions:

- "Koov, what is visible on the whiteboard?"
- "Koov, read the text I'm showing."
- "Koov, summarize this diagram."
- "Koov, what object am I holding?"

Explicitly unsupported:

- Identifying a person from their face.
- Comparing faces or recognizing returning participants.
- Emotion, health, age, ethnicity, or other sensitive-trait inference.
- Surveillance or continuous monitoring.
- CAPTCHA solving.
- Claims of certainty where the image is unclear.

### 14.2 Capture pipeline

Add `koov/facetime_vision.py`:

1. Verify vision is globally and per-meeting enabled.
2. Verify the worker is in call and the request is fresh.
3. Capture a PNG with Selenium in memory.
4. Detect candidate visible `<video>` rectangles and the main call-content region.
5. Crop to the requested/most relevant region when reliable; otherwise use the sanitized call viewport.
6. Reject blank, mostly uniform, tiny, or stale captures.
7. Resize to a configured maximum while preserving aspect ratio.
8. Encode in memory and call a configured vision-capable model with the user's exact question plus a narrow safety/system instruction.
9. Return the answer to the request result file and optionally pass it to the existing response/TTS path.
10. Drop all image bytes and temporary objects after the request.

Avoid `getDisplayMedia`: it requires explicit user selection each time and is for screen surfaces, while the worker already has a rendered browser viewport. Selenium capture is the appropriate source here.

### 14.3 Vision command queue

Paths:

```text
/notes/worker-commands-<meeting_id>/pending/<request_id>.json
/notes/worker-commands-<meeting_id>/processing/<request_id>.json
/notes/worker-commands-<meeting_id>/done/<request_id>.json
```

Request schema:

```json
{
  "schema_version": 1,
  "request_id": "uuid",
  "kind": "vision_query",
  "meeting_id": "meeting-123",
  "question": "What is written on the whiteboard?",
  "requested_at": 1794680000.0,
  "expires_at": 1794680030.0
}
```

Worker claims a request with an atomic rename from `pending` to `processing`. On restart, stale `processing` items are either returned to pending once or failed explicitly. Results contain text and timing only, never image data.

Add:

```http
POST /api/meetings/<meeting_id>/vision/query
GET  /api/meetings/<meeting_id>/vision/requests/<request_id>
```

The POST returns 202 and a request ID. The GET reports queued, processing, completed, expired, or failed. Cap questions at 500 characters and enforce per-meeting rate limits.

### 14.4 Voice integration

Do not initially send every wake request to vision. Add a narrow visual-intent detector for phrases containing both a visual action and a visible target, such as `look`, `see`, `read`, `whiteboard`, `screen`, `diagram`, `camera`, or `showing`.

When confidence is low, Koov should ask a short clarification rather than taking a screenshot. A voice-triggered visual request must follow the same feature gates, rate limits, no-retention policy, and status reporting as an API request.

### 14.5 Privacy and retention

- Default `KOOV_FT_VISION_ENABLED=0`.
- Default image retention is zero.
- Do not write captures to the notes volume in normal operation.
- Diagnostic capture requires a separate explicit debug flag and must redact/expire files.
- Status records request ID, timestamps, outcome, latency, and error category only.
- Provide a visible camera card or audible disclosure when vision becomes enabled for a meeting.
- Ensure logs never include base64 image data or full model request bodies.

## 15. Worker integration

Integrate in `koov/facetime_worker.py` in small stages:

1. Parse and validate camera/vision environment settings.
2. Initialize camera state variables before browser startup.
3. Start `VirtualCameraOutput`; write early health status.
4. Launch Chromium and preserve existing audio flags.
5. Grant permissions and enumerate devices.
6. After admission, run the camera controller asynchronously.
7. Poll desired-state revision at approximately 2 Hz using cheap file metadata checks.
8. Poll vision commands only when vision is enabled.
9. Read `KoovAgent.get_runtime_activity()` for automatic animation.
10. Add camera health to periodic status writes.
11. During shutdown, stop accepting commands, finish/fail an active visual request, turn camera off best-effort, quit Chromium, stop the video producer, and release the device through API/container cleanup.

Never perform model inference or blocking FFmpeg restart work on the main FaceTime state loop. Use bounded worker threads and report their health.

## 16. Error model

Use stable error codes in status and logs:

| Code | Meaning | Required behavior |
|---|---|---|
| `camera_disabled` | Feature globally/per-meeting disabled | Continue audio-only |
| `camera_pool_exhausted` | No lease available | Continue audio-only unless required |
| `camera_device_missing` | Assigned device absent in container | Continue audio-only |
| `camera_device_busy` | Device already opened incorrectly | Release/reconcile; audio-only |
| `camera_producer_failed` | FFmpeg could not stay alive | Retry three times, then audio-only |
| `camera_not_enumerated` | Chromium cannot see device | Diagnostic capture; audio-only |
| `camera_selector_failed` | FaceTime UI selection failed | Bounded retry; audio-only |
| `camera_toggle_failed` | Selected but not enabled | Report desired/actual mismatch |
| `vision_disabled` | Visual query not allowed | Reject without capture |
| `vision_rate_limited` | Too many visual requests | Fail request without capture |
| `vision_blank_frame` | No usable rendered content | Do not call model |
| `vision_model_failed` | Timeout/provider/model error | Report uncertainty; no fabricated answer |

Error messages exposed through the API must be safe and bounded. Full subprocess stderr belongs only in capped diagnostic logs.

## 17. Observability

Add structured FaceTime steps:

- `camera_lease_acquired`
- `camera_producer_started`
- `camera_device_enumerated`
- `camera_selected`
- `camera_enabled`
- `camera_scene_changed`
- `camera_repair`
- `camera_degraded_audio_only`
- `vision_requested`
- `vision_frame_captured`
- `vision_completed`
- `vision_failed`

Record durations and counters without high-cardinality image/device identifiers. Useful metrics:

- Active/available camera devices.
- Lease acquisition failures.
- Producer restarts.
- Desired/actual camera mismatch duration.
- Time from admission to selected camera.
- Time from command acceptance to remote state change.
- Vision request count, latency, failure code, and rate-limit count.
- Incremental worker CPU and memory with camera enabled versus disabled.

## 18. Test plan

### 18.1 Baseline gate before implementation

Run the current FaceTime/audio tests in the same dependency environment used by the worker:

```bash
PYTHONPATH=. pytest -q \
  tests/test_facetime_join_guards.py \
  tests/test_facetime_worker_status.py \
  tests/test_facetime_tts_routing.py \
  tests/test_join_backend_inference.py
```

The current local shell lacks application test dependencies such as `flask_cors` and `webrtcvad`, so local collection is not a valid baseline until dependencies are installed or tests are run inside the built image. Record the worker-image result before changing camera code.

### 18.2 New unit tests

Add:

- `tests/test_facetime_camera_config.py`
  - Defaults off.
  - Boolean/number parsing.
  - Invalid dimensions, FPS, modes, and device strings.
- `tests/test_facetime_camera_leases.py`
  - Unique allocation.
  - Pool exhaustion.
  - Release and reuse.
  - Concurrent acquisition under file lock.
  - Docker-label reconciliation.
  - Failed-start cleanup.
- `tests/test_facetime_camera_control.py`
  - Atomic writes and revision ordering.
  - Idempotent replay.
  - Expired cards.
  - Text/mode/duration validation.
- `tests/test_facetime_video.py`
  - Every scene renders expected dimensions/type.
  - Long/Unicode text is bounded.
  - Animation state does not mutate input.
  - Latest-frame/drop behavior.
  - Producer restart and shutdown with mocked subprocess.
- `tests/test_facetime_camera_dom.py`
  - Camera label matching.
  - Menu-item rejection for ambiguous labels.
  - Toggle-state interpretation.
  - Sanitized diagnostic metadata.
- `tests/test_facetime_camera_api.py`
  - Auth behavior.
  - FaceTime-only behavior.
  - Status propagation.
  - Multi-bot targeting.
  - Global-disable behavior.
- `tests/test_facetime_vision.py`
  - Feature gates and rate limits.
  - Blank-frame rejection.
  - Crop bounds.
  - No image bytes in result/log/status.
  - Provider timeouts and malformed responses.
- `tests/test_facetime_vision_queue.py`
  - Atomic claim.
  - Duplicate request handling.
  - Expiry and stale-processing recovery.

### 18.3 Container integration tests

On Linux with a disposable V4L2 device:

1. Start the renderer into the loopback device.
2. Run Chromium in the worker image with camera permission.
3. Assert `enumerateDevices()` includes one labeled `videoinput`.
4. Acquire a stream and sample a frame.
5. Compare the frame against a known test pattern within tolerance.
6. Simultaneously acquire the real PulseAudio virtual microphone and prove it remains the expected source.
7. Kill FFmpeg and verify bounded restart and recovery.
8. Remove/deny the device and verify audio-only fallback.

Mark this suite with a dedicated integration marker and skip with an explicit reason on macOS or hosts without the module. It must be required in Linux deployment CI/staging.

### 18.4 API/worker integration tests

- Start API and a fake worker sharing a temporary notes directory.
- Post camera state and verify atomic revision acknowledgement.
- Simulate API restart and reconstruct session camera status from disk.
- Simulate worker restart and reapply desired state.
- Submit a visual request and verify pending -> processing -> done.
- Stop the session while a visual request is active and verify a terminal result.
- Start more meetings than devices and verify documented required/optional behavior.

### 18.5 Real FaceTime acceptance tests

Use a dedicated test FaceTime link and two physical Apple devices.

#### Test A: outbound camera

- Join with camera disabled; remote sees no Koov video.
- Turn on avatar; remote sees the correct avatar within five seconds.
- Switch listening/thinking/speaking/status-card modes.
- Turn camera off and back on.
- Leave connected for at least 30 minutes.

#### Test B: audio regression

- Participant speech is transcribed.
- Wake Koov and receive a relevant spoken response.
- Remote participants hear the complete TTS response.
- Barge-in/stop behavior remains correct.
- Camera state changes while TTS is playing do not create audio gaps.

#### Test C: reconnection and repair

- Admit after an extended waiting-room delay.
- Toggle camera manually in FaceTime and verify desired state is repaired.
- Force producer exit and verify recovery or clean audio-only degradation.
- Restart the API while the worker remains active and verify lease/status reconstruction.

#### Test D: isolation

- Start two simultaneous FaceTime workers on different calls.
- Display distinct cards containing non-sensitive test markers.
- Verify each call sees only its own marker.
- Stop one worker and verify the other remains unchanged.
- Start a replacement worker and verify safe device reuse.

#### Test E: vision

- Show large printed text and ask Koov to read it.
- Show a simple diagram and ask for a summary.
- Ask a nonvisual question and verify no screenshot is taken.
- Present a blank/dark feed and verify Koov reports that it cannot see enough.
- Verify the notes directory contains no captured frame after completion.

### 18.6 Performance gates

Measure camera-disabled and camera-enabled versions on the same VM:

- Camera pipeline incremental CPU target: no more than 20% of one vCPU at 640x360/12 FPS during a static avatar.
- Incremental resident memory target: below 150 MB per worker.
- No unbounded FFmpeg stderr, frame, command, or screenshot accumulation.
- Camera command acknowledgement target: under one second locally.
- Remote visible transition target: under five seconds.
- Vision requests must not block worker polling or audio processing.
- Run at configured maximum concurrency for at least 30 minutes with no cross-session output.

## 19. Implementation phases and merge gates

### Phase 0: deployment-host feasibility spike

Deliverables:

- Host setup and verification scripts in a draft branch.
- One manually provisioned V4L2 device.
- Test pattern visible in Chromium and a real FaceTime call.
- Proof that the current PulseAudio microphone still carries Koov TTS.
- Proof that a Selenium screenshot contains rendered remote video.
- Recorded supported resolution/pixel format and resource usage.

Exit gate: all five proofs pass. If the VM forbids kernel modules or device pass-through, stop and move FaceTime workers to a dedicated Linux VM. Do not proceed with a brittle browser monkeypatch as the default architecture.

### Phase 1: outbound-camera foundation

Deliverables:

- Lease manager and centralized Docker command builder.
- FFmpeg/v4l-utils worker image.
- Renderer/producer module.
- Camera controller.
- Worker status fields.
- Camera off by default.

Exit gate: unit, container integration, Test A, Test B, and one-worker failure tests pass.

### Phase 2: controls and concurrency

Deliverables:

- Desired-state protocol.
- Camera API endpoint.
- Multi-bot leases/status.
- API-restart reconciliation.
- Dashboard API contract documentation.

Exit gate: API integration, Test C, Test D, and maximum-concurrency performance tests pass.

### Phase 3: visual understanding

Deliverables:

- Visual command queue.
- Screenshot/crop/validation pipeline.
- Configurable vision-model client.
- API query/result routes.
- Narrow voice intent.
- Retention and disclosure controls.

Exit gate: vision unit/integration tests, Test E, security review, and no-retention verification pass.

### Phase 4: production rollout

Deliverables:

- Deployment preflight.
- Metrics/alerts.
- Runbook and rollback instructions.
- Canary results.

Exit gate: one canary worker, then 25%, then 100% of desired meetings with no audio regression or isolation incident.

## 20. Rollout and rollback

### 20.1 Rollout sequence

1. Deploy code with `KOOV_FT_CAMERA_FEATURE=0` and `KOOV_FT_VISION_ENABLED=0`.
2. Verify the existing audio-only FaceTime flow.
3. Provision the host device pool.
4. Enable camera globally but keep per-meeting camera off.
5. Enable one internal test meeting.
6. Enable camera for a small canary set.
7. Enable automatic avatar/activity mode only after stability data is acceptable.
8. Roll out vision separately, still default off.

### 20.2 Rollback

- Set `KOOV_FT_CAMERA_FEATURE=0`; new workers start audio-only without code rollback.
- Set `KOOV_FT_VISION_ENABLED=0`; all new visual requests are rejected before capture.
- Existing V4L2 host devices may remain loaded; unused devices are inert.
- If necessary, roll back the application image while leaving host provisioning intact.
- Never unload the module while active workers own devices.
- Reconcile and clear only stale lease metadata after confirming no labeled worker containers are running.

## 21. Security review checklist

- `KOOV_API_KEY` required in production when camera or vision is enabled.
- CORS restricted to the production dashboard origin.
- Device paths come only from server configuration.
- No `shell=True`, user-supplied command arguments, or remote image URLs.
- Camera text length, mode, duration, and meeting IDs validated.
- Worker command files written atomically and scoped by meeting.
- No frame/image data in API status, logs, Supabase, transcripts, or meeting summaries.
- No raw screenshot persistence by default.
- No face recognition or sensitive visual inference.
- Rate limits and timeouts on visual requests.
- Model answers treated as fallible; unclear images produce uncertainty.
- Multi-bot and cross-tenant isolation tested before release.

## 22. Documentation deliverables

Update or add:

- `docs/FACETIME_JOIN.md` — feature flags, join request, status fields, audio-only fallback.
- `docs/FACETIME_CAMERA_RUNBOOK.md` — host setup, verification, capacity, troubleshooting, rollback.
- `.env.example` — all camera/vision settings with safe defaults.
- API examples for camera control and visual query polling.
- Dashboard guidance: show desired versus actual state and never infer success solely from HTTP 202.

## 23. Estimated effort

| Work | Estimate |
|---|---:|
| Host spike and provisioning | 1–2 engineering days |
| Lease manager, container plumbing, renderer | 3–4 days |
| FaceTime UI selection and repair | 2–3 days |
| Control API, status, concurrency | 2–3 days |
| Visual understanding and voice intent | 3–5 days |
| E2E, performance, security, deployment | 3–5 days |

Expected total: approximately 14–22 engineering days, dominated by real FaceTime UI validation and Linux-host testing rather than frame rendering.

## 24. First implementation checkpoint

Before writing the production renderer or API routes, complete Phase 0 on the deployment VM and record:

- Linux distribution and kernel version.
- Whether matching headers and `v4l2loopback` load successfully.
- Secure Boot/module-signing status.
- Device numbers and labels.
- Supported pixel formats/resolutions.
- Whether Docker `--device` exposes the device to the worker image.
- Whether Chromium enumerates it after the producer opens it.
- Whether FaceTime transmits the test pattern.
- Whether Koov's current microphone and TTS remain intact.
- Whether Selenium screenshots include remote video.

Those facts are the hard feasibility gate. Once they pass, implementation proceeds phase-by-phase with camera and vision feature flags off until their respective acceptance suites pass.
