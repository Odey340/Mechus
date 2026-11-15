# FaceTime Virtual Camera Runbook

Operating guide for the FaceTime bot's generated camera and visual questions. For the API contract, see [FACETIME_JOIN.md](FACETIME_JOIN.md#camera-and-visual-questions-optional). For the design, see [FACETIME_CAMERA_IMPLEMENTATION_PLAN.md](FACETIME_CAMERA_IMPLEMENTATION_PLAN.md).

**Safety rule:** with `MECHUS_FT_CAMERA_FEATURE=0` (the default) nothing in this document matters, and the bot is audio-only exactly as before. Every camera failure also falls back to audio-only.

## How it fits together

```
host kernel: v4l2loopback  ->  /dev/video20 … /dev/video23   ("Mechus Virtual Camera N", exclusive_caps=1)
API container: leases one device per FaceTime worker (notes/.facetime-camera-leases.json)
worker container: --device /dev/video20  ->  Python renderer -> ffmpeg -> /dev/video20 -> Chromium -> FaceTime
```

Docker Desktop on macOS cannot load v4l2loopback. Everything below runs on the **Linux deployment VM**.

## 1. One-time host setup

```bash
cd ~/mechusdemo   # repo checkout on the VM
sudo MECHUS_FT_VIDEO_DEVICES=/dev/video20,/dev/video21,/dev/video22,/dev/video23 \
  scripts/setup_facetime_cameras.sh
```

The script:
- installs `linux-headers-$(uname -r)`, `v4l2loopback-dkms`, and `v4l-utils` (Debian/Ubuntu)
- writes `/etc/modprobe.d/mechus-facetime-camera.conf` and `/etc/modules-load.d/mechus-facetime-camera.conf` so the devices come back after a reboot
- loads the module, then runs the verifier

If the module is already loaded with different options, the script refuses to unload it, because active calls may own devices. Reboot in a maintenance window instead.

**Secure Boot:** if `modprobe` fails and `mokutil --sb-state` reports enabled, the DKMS module must be signed. Run `sudo mokutil --import /var/lib/dkms/mok.pub`, reboot, and enroll the key in the MOK manager. Do not disable Secure Boot.

**Cloud VMs:** some kernels (for example, `linux-*-cloud` minimal flavours) ship without V4L2. If `modprobe` reports "Unknown symbol" or the headers package doesn't exist, install the generic kernel or use a different VM image. This is the hard feasibility gate.

## 2. Verify the host (read-only)

```bash
MECHUS_FT_VIDEO_DEVICES=/dev/video20,/dev/video21,/dev/video22,/dev/video23 \
  scripts/verify_facetime_camera_host.sh
```

| Exit code | Meaning |
|---|---|
| 0 | Ready |
| 2 | Not Linux |
| 3 | Module not loaded |
| 4/5 | Pool empty or invalid path |
| 6 | Device node missing |
| 7 | Device not readable/writable |
| 8 | Docker missing |

## 3. Phase 0 probe (frames reach Chromium)

```bash
docker build -t mechus-facetime-worker:latest -f docker/facetime-worker/Dockerfile .
scripts/facetime_camera_spike.sh          # full probe, including an ffmpeg kill/restart check
scripts/facetime_camera_spike.sh --quick  # what restart-on-server.sh runs
```

The probe runs in the real worker image, with the same PulseAudio and Xvfb setup, and prints JSON. Every check must be `"ok": true`:

| Check | Proves |
|---|---|
| `device_present`, `v4l2_info` | Docker passed the device through |
| `producer_started` | ffmpeg accepts the YUYV stream |
| `chromium_enumerates_and_streams` | Chromium sees exactly one "Mechus" camera and can open it |
| `frame_matches_test_pattern` | The pixels Chromium receives are the magenta test pattern |
| `pulse_mic_unaffected` | The Pulse microphone still works alongside the camera |
| `producer_restarts_after_kill` | Supervision recovers a crashed ffmpeg |
| `device_released` | Nothing holds the device after exit |

Then run one real call by hand (§18.5 Test A in the plan). Join with `"camera":{"enabled":true}`, admit Mechus, and confirm a second Apple device sees the avatar and still hears Mechus's replies.

## 4. Enable

In `.env` (or the shell before `scripts/restart-on-server.sh`):

```dotenv
MECHUS_API_KEY=<set this>
MECHUS_FT_CAMERA_FEATURE=1
MECHUS_FT_VIDEO_DEVICES=/dev/video20,/dev/video21,/dev/video22,/dev/video23
MECHUS_FT_CAMERA_AUTO_ON=0          # turn on per meeting, or via the camera API
# MECHUS_FT_VISION_ENABLED=1        # roll out separately, after the camera is stable
```

With the feature on, `restart-on-server.sh` runs the verifier before rebuilding (deploy fails if the host isn't ready) and the quick probe after building (warning only).

Recommended rollout: feature on with auto-on off, then one internal meeting, then a small canary, then `activity` mode by default. Enable vision last.

## 5. Capacity

- One device per **concurrent** FaceTime bot (multi-bot meetings use one per bot). Size the pool to peak concurrency plus one.
- When the pool is exhausted, extra bots join audio-only with `camera_pool_exhausted`, unless the join set `camera.required`, in which case that join errors.
- Budget roughly 10–20% of one vCPU and under 150 MB per camera-enabled worker at 640×360 / 12 fps. Measure on your VM with `docker stats` and compare with the feature off.

## 6. Troubleshooting by `facetime_camera_error_code`

| Code | Likely cause | Action |
|---|---|---|
| `camera_pool_exhausted` | More concurrent bots than devices | Grow `MECHUS_FT_VIDEO_DEVICES` (and re-run setup), or wait |
| `camera_device_missing` | Module not loaded, device number changed, or `--device` failed | `verify_facetime_camera_host.sh`; check `docker inspect <worker>` for the device |
| `camera_producer_failed` | ffmpeg can't write to the device (format, busy) | `scripts/facetime_camera_spike.sh`; `fuser /dev/videoN`; check worker logs for ffmpeg stderr |
| `camera_not_enumerated` | Chromium doesn't list the camera (`exclusive_caps` missing, or the producer started late) | Confirm `exclusive_caps=1` in `/sys/module/v4l2loopback/parameters/`; run the probe |
| `camera_selector_failed` / `camera_toggle_failed` | FaceTime web UI changed | Open `mechus_notes/facetime-<id>-camera-dom.html` (sanitized markup saved once per meeting) and update the labels in `mechus/facetime_camera.py` (`classify_camera_button`) plus `tests/test_facetime_camera_dom.py` |
| `camera_control_invalid` | Malformed `camera-control-<id>.json` | Re-send a command through the API (it rewrites the file atomically) |

Leases: `mechus_notes/.facetime-camera-leases.json`. They are reconciled against `docker ps --filter label=mechus.facetime=1` on every join and after orphan cleanup. Leases younger than 3 minutes survive reconciliation, so a join that is still starting keeps its device. Do not edit the file by hand while workers are running.

Vision failures appear as `facetime_vision_last_error` and on the request result (`vision_blank_frame`, `vision_model_failed`, `vision_rate_limited`, `vision_timeout`, `vision_meeting_ended`, `vision_worker_restarted`).

## 7. Rollback

1. Set `MECHUS_FT_CAMERA_FEATURE=0` (and/or `MECHUS_FT_VISION_ENABLED=0`) and restart the API. New workers are audio-only. No code rollback is needed.
2. Running workers keep their camera until the meeting ends.
3. Leave the kernel module loaded. Unused devices are inert. Never unload it while a worker owns a device (`fuser /dev/video2*`).
4. To remove completely after all workers stop: `sudo rm /etc/modprobe.d/mechus-facetime-camera.conf /etc/modules-load.d/mechus-facetime-camera.conf && sudo modprobe -r v4l2loopback`.
