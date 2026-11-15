"""Mechus Chert FaceTime worker — runs one FaceTime call via the Chert FaceTime API.

Launched as a subprocess by api_server.py with ``MECHUS_JOIN_BACKEND=facetime_chert``.
Replaces the Chromium/Selenium/PulseAudio ``facetime_web`` worker: no browser, no
container, no host-admit scraping.

Flow:
  1. Join a LiveKit room and publish Mechus's audio track.
  2. Ask Chert to place the FaceTime call and bridge it into that room.
  3. When the caller's audio track appears, run MechusAgent on LiveKit audio in/out.
  4. On stop or hang-up: end the Chert call, write the final summary, exit.

Status is written to ``<MECHUS_NOTES_DIR>/worker-status-<meeting_id>.json``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

AGENT_IDENTITY = "mechus-agent"
BRIDGE_IDENTITY = "chert-bridge"


def _write_status(path: str, data: dict[str, Any]) -> None:
    """Atomic write via temp-file rename to avoid partial reads."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass  # best-effort; coordinator falls back to in-memory state


def _apply_env_defaults() -> None:
    """FaceTime voice-agent tuning, minus everything PulseAudio/Chromium-specific."""
    from mechus.facetime_env import (
        PRODUCTION_WAKE_PHRASE,
        apply_facetime_eleven_env_defaults,
        apply_facetime_worker_env_defaults,
    )

    os.environ["MECHUS_JOIN_BACKEND"] = "facetime_chert"
    os.environ.setdefault("MECHUS_WAKE_PHRASE", PRODUCTION_WAKE_PHRASE)
    if not (os.environ.get("MECHUS_BOT_PERSONA_ID") or "").strip():
        os.environ.setdefault("MECHUS_PERSONAS_ENABLED", "default")
    os.environ["MECHUS_CONVERSATIONAL"] = "0"
    os.environ["MECHUS_LOW_LATENCY"] = "1"
    apply_facetime_eleven_env_defaults()
    apply_facetime_worker_env_defaults()
    # Multilingual STT: Scribe handles accents + 90+ languages; local base.en cannot.
    if (os.environ.get("MECHUS_ELEVEN_API_KEY") or "").strip():
        os.environ["MECHUS_STT_BACKEND"] = "elevenlabs"
    else:
        _wm = (os.environ.get("MECHUS_WHISPER_MODEL") or "").strip().lower()
        if _wm in ("", "base.en", "base"):
            os.environ["MECHUS_WHISPER_MODEL"] = "small"
    os.environ.pop("MECHUS_WHISPER_LANGUAGE", None)
    # The caller's audio can carry an echo of Mechus's own voice, so raw-VAD barge-in stays
    # off; only an explicit "Mechus, stop" in the transcript cuts a reply.
    os.environ["MECHUS_INTERRUPT_ON_SPEECH"] = "0"
    os.environ["MECHUS_INTERRUPT_ON_SPEECH_ZOOM"] = "0"
    os.environ["MECHUS_TRANSCRIPT_BARGE_IN"] = "1"
    os.environ["MECHUS_TRANSCRIPT_BARGE_IN_STOP_ONLY"] = "1"
    os.environ.setdefault("MECHUS_RESPONSE_TEMPERATURE", "0.35")
    os.environ.setdefault("MECHUS_WAKE_TRANSCRIPT_MAX_DIST", "1")
    os.environ.setdefault("MECHUS_FOLLOWUP_WINDOW_S", "12")
    os.environ.setdefault("MECHUS_RESPONSE_OPENAI_TIMEOUT_S", "12")
    os.environ.setdefault("MECHUS_RESPONSE_PROMPT_MAX_CHARS", "7000")
    os.environ.setdefault("MECHUS_RETRIEVAL_MAX_CHARS", "2800")
    # One mixed audio track from Chert: no per-speaker streams.
    os.environ.setdefault("MECHUS_ZOOM_STREAM_ATTRIBUTION", "0")
    if os.environ.get("MECHUS_DIARIZATION_ENABLED") is None:
        os.environ["MECHUS_DIARIZATION_ENABLED"] = "0"


def main() -> None:
    from mechus.logging import setup_logging

    setup_logging(os.environ.get("MECHUS_LOG_LEVEL", "INFO"))
    log = logging.getLogger(__name__)

    meeting_id = os.environ.get("MECHUS_MEETING_ID", "").strip()
    # For Chert the "meeting link" is the destination: a FaceTime link, phone number or email.
    destination_raw = (
        os.environ.get("MECHUS_CHERT_DESTINATION") or os.environ.get("MECHUS_MEETING_LINK") or ""
    ).strip()
    if not meeting_id or not destination_raw:
        sys.stderr.write(
            "[chert_worker] MECHUS_MEETING_ID and MECHUS_MEETING_LINK (or MECHUS_CHERT_DESTINATION) must be set\n"
        )
        sys.exit(1)

    team_id = os.environ.get("MECHUS_TEAM_ID", "").strip()
    shared_context_enabled = os.environ.get("MECHUS_SHARED_CONTEXT_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
    participant_timezones_json = os.environ.get("MECHUS_PARTICIPANT_TIMEZONES_JSON", "").strip()

    from mechus.envfile import load_env_file
    from mechus.notes_paths import apply_notes_paths_to_env

    load_env_file(os.environ.get("MECHUS_ENV_FILE", ".env"))
    notes_dir, _store_dir = apply_notes_paths_to_env()
    status_path = os.path.join(notes_dir, f"worker-status-{meeting_id}.json")

    from mechus.facetime_worker import facetime_dashboard_labels

    call_info: dict[str, Any] = {}

    def write(status: str, **extra: object) -> None:
        payload: dict[str, object] = {
            "status": status,
            "meeting_id": meeting_id,
            "ts": time.time(),
            "meeting_link": destination_raw,
            "join_backend": "facetime_chert",
            **call_info,
            **extra,
        }
        _fs = str(payload.get("facetime_state") or "").strip()
        if _fs:
            payload.update(facetime_dashboard_labels(_fs))
        _write_status(status_path, payload)

    write("starting", facetime_state="starting")
    log.info("Chert worker started: meeting_id=%s", meeting_id)

    def fail(message: str, *, code: int = 1) -> None:
        log.error("Chert worker: %s (meeting_id=%s)", message, meeting_id)
        write("error", error=message, facetime_state="error")
        sys.exit(code)

    # ── Config ───────────────────────────────────────────────────────────────
    _apply_env_defaults()

    from mechus.chert_client import (
        MAX_BRIDGE_TOKEN_TTL_S,
        ChertClient,
        ChertConfig,
        ChertError,
        call_id_from_envelope,
        call_status_from_envelope,
        classify_destination,
        is_terminal_status,
        livekit_sdk_available_reason,
        mint_livekit_token,
        room_name_for_meeting,
    )

    chert_cfg = ChertConfig.from_env()
    missing = chert_cfg.missing()
    if missing:
        fail("Chert FaceTime is not configured; missing env: " + ", ".join(missing))
    lk_ok, lk_reason = livekit_sdk_available_reason()
    if not lk_ok:
        fail(f"LiveKit SDK unavailable ({lk_reason}); pip install -r requirements-chert.txt")

    try:
        destination_type, destination = classify_destination(destination_raw)
    except ValueError as exc:
        fail(str(exc))
        return
    _forced_type = (os.environ.get("MECHUS_CHERT_DESTINATION_TYPE") or "").strip().lower()
    if _forced_type in ("phone", "email", "facetime_link"):
        destination_type = _forced_type

    from mechus.config import MechusConfig

    config = MechusConfig.from_env()

    # ── Signals (registered early: a stop during dialing must still hang up) ──
    stop_event = threading.Event()
    agent_holder: dict[str, Any] = {"agent": None}

    def _on_signal(signum: int, frame: object) -> None:
        log.info("Chert worker received signal %s for meeting_id=%s; stopping", signum, meeting_id)
        stop_event.set()
        try:
            a = agent_holder.get("agent")
            if a is not None:
                a.stop()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # ── 1. LiveKit room ──────────────────────────────────────────────────────
    from mechus.chert_livekit import ChertLiveKitSession

    def _brand_name() -> str:
        from mechus.brand import ASSISTANT_NAME

        return ASSISTANT_NAME

    room_name = room_name_for_meeting(meeting_id)
    # MECHUS_CHERT_VIDEO=1: FaceTime Video call; Mechus subscribes to the caller's camera.
    video_call = (os.environ.get("MECHUS_CHERT_VIDEO") or "").strip().lower() in ("1", "true", "yes", "on")
    # MECHUS_INSPECT_MODE=move_in|move_out: Mechus watches the camera and calls out damage on
    # its own. No wake word on these calls; the wake/transcript agent is not run.
    from mechus.inspection import inspect_mode_from_env, inspect_unit_from_env

    inspect_mode = inspect_mode_from_env()
    inspect_unit = inspect_unit_from_env()
    if inspect_mode:
        video_call = True
    frame_path = os.path.join(notes_dir, f"chert-frame-{meeting_id}.jpg")
    last_frame_saved = 0.0
    agent_token = mint_livekit_token(
        api_key=chert_cfg.livekit_api_key,
        api_secret=chert_cfg.livekit_api_secret,
        identity=AGENT_IDENTITY,
        room=room_name,
        ttl_s=6 * 3600,
        name=os.environ.get("MECHUS_DISPLAY_NAME", "").strip() or _brand_name(),
    )
    session = ChertLiveKitSession(
        url=chert_cfg.livekit_url,
        token=agent_token,
        agent_identity=AGENT_IDENTITY,
        bridge_identity=BRIDGE_IDENTITY,
        sample_rate_hz=config.sample_rate_hz,
        frame_ms=config.frame_ms,
        video=video_call,
    )
    write("joining", facetime_state="opening_link", join_state="connecting_room")
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        session.close()
        fail(f"could not join LiveKit room: {type(exc).__name__}: {exc}")
    call_info["livekit_room"] = room_name
    if video_call:
        session.update_video_card(
            issue_count=0,
            mode=inspect_mode or "VIDEO",
            unit=inspect_unit or "Live FaceTime video",
            status="Waiting for FaceTime camera",
        )

    # ── MechusAgent boots in the background while the call is being placed ─────
    agent_boot_error: list[str] = []

    def _boot_agent() -> None:
        if inspect_mode:
            # Connecting to MongoDB Atlas takes several seconds; do it while the phone rings.
            try:
                from mechus.inspection import make_store

                agent_holder["inspection_store"] = make_store(os.path.join(notes_dir, "inspections"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Chert worker: inspection store failed to open: %s", exc)
        try:
            from mechus.agent import MechusAgent

            agent = MechusAgent(config, audio_in=session.audio_in, audio_out=session.audio_out)
            _pinned = (os.environ.get("MECHUS_MEMORY_MEETING_IDS_JSON") or "").strip()
            if _pinned:
                try:
                    _ids = json.loads(_pinned)
                    if isinstance(_ids, list):
                        agent._pinned_memory_ids = [str(m).strip() for m in _ids if m]
                except Exception:
                    pass
            agent.set_meeting(meeting_id)
            if team_id and shared_context_enabled:
                agent.set_team_context(team_id)
            if participant_timezones_json:
                try:
                    _tz = json.loads(participant_timezones_json)
                    if isinstance(_tz, dict):
                        agent.merge_participant_timezones({str(k): str(v) for k, v in _tz.items()})
                except Exception as exc:  # noqa: BLE001
                    log.warning("Chert worker participant timezones parse failed: %s", exc)
            if video_call and not inspect_mode:
                def _latest_camera_jpeg() -> bytes | None:
                    snap = session.latest_frame_jpeg(max_side=768)
                    # Ignore stale frames (camera off / track gone).
                    if snap is None or time.time() - snap[1] > 5.0:
                        return None
                    # Keep exactly what the vision model was shown, for debugging.
                    try:
                        _n = int(call_info.get("chert_vision_frames", 0) or 0) + 1
                        call_info["chert_vision_frames"] = _n
                        with open(
                            os.path.join(notes_dir, f"chert-vision-{meeting_id}-{_n:02d}.jpg"), "wb"
                        ) as _vf:
                            _vf.write(snap[0])
                    except Exception:  # noqa: BLE001
                        pass
                    return snap[0]

                agent._vision_frame_provider = _latest_camera_jpeg
            agent_holder["agent"] = agent
            # Chert's Mac hangs up a call that hears nothing within 10 s of bridge_ready,
            # so synthesize the greeting now and have it ready the moment the call connects.
            from mechus.brand import ASSISTANT_NAME

            _default_greeting = f"Hey, it's {ASSISTANT_NAME}. Say {ASSISTANT_NAME}, then ask me anything."
            if inspect_mode == "move_in":
                _default_greeting = (
                    f"Hi, this is {ASSISTANT_NAME}, your property inspector. I'm going to guide "
                    "you through a move-in tour, one room at a time, and put anything I find "
                    "on the record. Turn on your back camera and follow my lead."
                )
            elif inspect_mode == "move_out":
                _default_greeting = (
                    f"Hi, this is {ASSISTANT_NAME}, your property inspector. I'm going to guide "
                    "you through your move-out check, one room at a time. Turn on your back "
                    "camera and follow my lead."
                )
            _greeting = os.environ.get("MECHUS_CHERT_GREETING", _default_greeting).strip()
            if _greeting:
                try:
                    agent_holder["greeting"] = agent._tts.synthesize(_greeting)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Chert worker: greeting synthesis failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Chert worker: MechusAgent failed to start")
            agent_boot_error.append(f"{type(exc).__name__}: {exc}")

    boot_thread = threading.Thread(target=_boot_agent, daemon=True, name=f"MechusAgentBoot-{meeting_id}")
    boot_thread.start()

    # ── 2. Place the FaceTime call ───────────────────────────────────────────
    client = ChertClient(chert_cfg)
    bridge_token = mint_livekit_token(
        api_key=chert_cfg.livekit_api_key,
        api_secret=chert_cfg.livekit_api_secret,
        identity=BRIDGE_IDENTITY,
        room=room_name,
        ttl_s=MAX_BRIDGE_TOKEN_TTL_S - 60,
        name="FaceTime",
    )
    call_id = ""

    def end_call() -> None:
        if not call_id:
            return
        try:
            client.end_call(call_id)
            log.info("Chert: ended call %s", call_id)
        except ChertError as exc:
            # invalid_state / not_found just mean the call was already over.
            log.info("Chert: end call %s: %s", call_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert: end call %s failed: %s", call_id, exc)

    def shutdown_early(message: str | None = None) -> None:
        end_call()
        session.close()
        if message:
            fail(message)
        write("stopped", facetime_state="stopped")
        sys.exit(0)

    write("joining", facetime_state="opening_link", join_state="dialing")
    try:
        envelope = client.create_call(
            destination=destination,
            destination_type=destination_type,
            participant_token=bridge_token,
            remote_participant_identity=AGENT_IDENTITY,
            video=video_call,
        )
    except ChertError as exc:
        session.close()
        fail(f"Chert refused the call: {exc}")
        return
    if envelope.get("_http_status") == 202 or envelope.get("queued"):
        session.close()
        fail("Chert line is busy (call was queued, not placed); try again shortly")
    call_id = call_id_from_envelope(envelope)
    call_info["chert_call_id"] = call_id
    call_info["chert_destination_type"] = destination_type
    log.info(
        "Chert: call allocated id=%s type=%s room=%s; waiting for answer",
        call_id or "?", destination_type, room_name,
    )

    # ── 3. Wait for the caller's audio (201 = allocated, not answered) ───────
    # A FaceTime link needs someone in the call to let Mechus in; a direct call just rings.
    waiting_state = "waiting_for_admit" if destination_type == "facetime_link" else "joining"
    answer_timeout_s = float(os.environ.get("MECHUS_CHERT_ANSWER_TIMEOUT_S", "120") or "120")
    t0 = time.monotonic()
    last_poll = 0.0
    while not session.remote_audio.is_set():
        if stop_event.is_set():
            shutdown_early()
        if session.call_ended.is_set():
            shutdown_early(f"call ended before it was answered ({session.end_reason or 'unknown'})")
        if (time.monotonic() - t0) > answer_timeout_s:
            shutdown_early(f"FaceTime call was not answered within {int(answer_timeout_s)}s")
        if agent_boot_error:
            shutdown_early(f"MechusAgent failed to start: {agent_boot_error[0]}")
        now = time.monotonic()
        if call_id and now - last_poll >= 3.0:
            last_poll = now
            try:
                c_status, c_reason = call_status_from_envelope(client.get_call(call_id))
            except Exception as exc:  # noqa: BLE001
                log.debug("Chert: call status poll failed: %s", exc)
                c_status, c_reason = "", ""
            if c_status:
                if c_status != call_info.get("chert_call_status"):
                    log.info("Chert: call status -> %s%s", c_status, f" ({c_reason})" if c_reason else "")
                call_info["chert_call_status"] = c_status
            if is_terminal_status(c_status):
                shutdown_early(
                    "FaceTime call did not connect: "
                    + c_status
                    + (f" ({c_reason})" if c_reason else "")
                )
        write(
            "joining",
            facetime_state=waiting_state,
            join_state="bridge_joined" if session.bridge_joined.is_set() else "ringing",
            facetime_ready=True,
        )
        time.sleep(0.25)

    # ── 4. Run the agent ─────────────────────────────────────────────────────
    boot_thread.join(timeout=180.0)
    agent = agent_holder.get("agent")
    if agent is None:
        shutdown_early(
            "MechusAgent failed to start: " + (agent_boot_error[0] if agent_boot_error else "boot timed out")
        )
        return

    write("active", facetime_state="in_call", facetime_ready=True)
    log.info("Chert worker active: meeting_id=%s call_id=%s", meeting_id, call_id)

    # Greet once FaceTime is connected (bridge_ready), not merely when the bridge's track
    # shows up. Wait briefly for the signal, then greet anyway: silence gets the call dropped.
    greeting = agent_holder.get("greeting")
    if greeting is not None:
        _g0 = time.monotonic()
        _ready = False
        while time.monotonic() - _g0 < 4.0 and not session.call_ended.is_set():
            if "chert_bridge_ready" in str(getattr(session, "room_metadata", "") or ""):
                _ready = True
                break
            try:
                _ev = json.dumps(client.get_call_events(call_id)) if call_id else ""
            except Exception:  # noqa: BLE001
                _ev = ""
            if "bridge_ready" in _ev or "call.started" in _ev:
                _ready = True
                break
            time.sleep(0.4)
        log.info("Chert: greeting caller (bridge_ready=%s, waited %.1fs)", _ready, time.monotonic() - _g0)
        try:
            session.audio_out.play(greeting.audio_f32, greeting.sample_rate_hz)
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert worker: greeting playback failed: %s", exc)

    inspector = None
    if inspect_mode:
        from mechus.inspection import Inspector, make_store

        def _inspect_frame() -> bytes | None:
            snap = session.latest_frame_jpeg(max_side=768)
            if snap is None or time.time() - snap[1] > 4.0:
                return None
            return snap[0]

        def _inspect_speak(sentence: str) -> None:
            audio = agent._tts.synthesize(sentence)
            session.audio_out.play_blocking(audio.audio_f32, audio.sample_rate_hz)

        # Let the greeting finish before the first finding can talk over it.
        try:
            session.audio_out.wait()
        except Exception:  # noqa: BLE001
            pass
        try:
            inspector = Inspector(
                mode=inspect_mode,
                unit=inspect_unit,
                meeting_id=meeting_id,
                get_frame=_inspect_frame,
                speak=_inspect_speak,
                store=agent_holder.get("inspection_store")
                or make_store(os.path.join(notes_dir, "inspections")),
                evidence_dir=os.path.join(notes_dir, "checks", meeting_id),
            )
        except Exception as exc:  # noqa: BLE001
            shutdown_early(f"inspection could not start: {type(exc).__name__}: {exc}")
            return
        inspector.start()
        agent_thread = inspector  # same is_alive()/join() surface as the agent thread
    else:
        agent_thread = threading.Thread(target=agent.run_forever, daemon=True, name=f"MechusAgent-{meeting_id}")
        agent_thread.start()

    transcript_chunks = 0
    while not stop_event.is_set():
        if not agent_thread.is_alive():
            log.info("Chert worker: agent thread exited for meeting_id=%s", meeting_id)
            break
        if session.call_ended.is_set():
            log.info(
                "Chert worker: call ended (%s) for meeting_id=%s", session.end_reason, meeting_id
            )
            break
        if video_call and time.monotonic() - last_frame_saved >= 2.0:
            last_frame_saved = time.monotonic()
            try:
                _snap = session.latest_frame_jpeg()
                if _snap is not None:
                    with open(frame_path + ".tmp", "wb") as _fh:
                        _fh.write(_snap[0])
                    os.replace(frame_path + ".tmp", frame_path)
                    if not call_info.get("chert_frame_path"):
                        log.info("Chert video: saved caller camera frame to %s", frame_path)
                    call_info["chert_frame_path"] = frame_path
            except Exception as exc:  # noqa: BLE001
                log.warning("Chert video: could not save frame: %s", exc)
        try:
            transcript_chunks = int(getattr(agent, "_transcript_chunk_id", 0) or 0)
        except Exception:
            pass
        transcript_path = ""
        try:
            p = agent._note_store.path or ""
            transcript_path = os.path.abspath(p) if p else ""
        except Exception:
            pass
        tz_map: dict = {}
        tz_conflicts: list = []
        try:
            tz_ctx = agent.get_timezone_context()
            tz_map = tz_ctx.get("participant_timezones") or {}
            tz_conflicts = tz_ctx.get("timezone_conflicts") or []
        except Exception:
            pass
        thoughts: list = []
        try:
            if hasattr(agent, "get_thoughts_for_status"):
                thoughts = agent.get_thoughts_for_status()
        except Exception:
            pass
        live_recap: dict = {}
        try:
            if hasattr(agent, "get_live_recap_for_dashboard"):
                live_recap = agent.get_live_recap_for_dashboard() or {}
        except Exception:
            pass
        last_audio = session.audio_in.last_frame_at
        receiving = bool(last_audio and (time.time() - last_audio) <= 15.0)
        if video_call:
            _issue_count = len(inspector.announced) if inspector is not None else 0
            session.update_video_card(
                issue_count=_issue_count,
                mode=inspect_mode or "VIDEO",
                unit=inspect_unit or "Live FaceTime video",
                status=inspector.card_status() if inspector is not None else "Live video connected",
            )
        write(
            "active",
            facetime_state="in_call",
            facetime_ready=True,
            transcript_chunks=transcript_chunks,
            last_activity=time.time(),
            transcript_path=transcript_path,
            team_id=team_id or None,
            shared_context=bool(team_id and shared_context_enabled),
            participants=session.participants_snapshot(),
            participant_timezones=tz_map,
            timezone_conflicts=tz_conflicts,
            speaker_attribution_mode="mixed",
            receiving_audio=receiving,
            last_audio_received_at=last_audio,
            audio_status="receiving" if receiving else ("no_audio_yet" if not last_audio else "silent"),
            thoughts=thoughts,
            live_recap=live_recap,
            inspection=inspector.status() if inspector is not None else None,
        )
        stop_event.wait(timeout=2.0)

    # ── 5. Shutdown: hang up first so the caller isn't left on a dead line ───
    if inspector is not None:
        inspector.stop()
    try:
        agent.stop()
    except Exception:
        pass
    end_call()
    if agent_thread.is_alive():
        log.info("Waiting for agent thread to finish (transcript flush to JSONL)...")
        agent_thread.join(timeout=8.0)
    try:
        agent._note_store.sync()
    except Exception:
        pass
    session.close()

    if inspector is not None:
        # Inspection calls have no transcript to summarise; the findings are the record.
        try:
            _report = os.path.join(notes_dir, f"inspection-{meeting_id}.json")
            with open(_report, "w", encoding="utf-8") as _rf:
                json.dump(inspector.status(), _rf, indent=2)
            log.info(
                "Inspection report: %d finding(s), %d frame(s) analysed -> %s",
                len(inspector.announced), inspector.frames_analysed, _report,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert worker: inspection report failed: %s", exc)
    else:
        try:
            from mechus.facetime_worker import _write_final_summary_on_shutdown

            _write_final_summary_on_shutdown(agent, config, meeting_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert worker: final summary failed: %s", exc)

    transcript_path_final = ""
    try:
        p = agent._note_store.path or ""
        transcript_path_final = os.path.abspath(p) if p else ""
    except Exception:
        pass
    final_thoughts: list = []
    try:
        if hasattr(agent, "get_thoughts_for_status"):
            final_thoughts = agent.get_thoughts_for_status()
    except Exception:
        pass
    # Keep transcript_path + meeting_link so GET /api/transcript works after the API drops the session.
    write(
        "stopped",
        facetime_state="ended" if session.call_ended.is_set() else "stopped",
        transcript_chunks=transcript_chunks,
        transcript_path=transcript_path_final,
        team_id=team_id or None,
        shared_context=bool(team_id and shared_context_enabled),
        thoughts=final_thoughts,
        inspection=inspector.status() if inspector is not None else None,
    )
    log.info("Chert worker stopped: meeting_id=%s", meeting_id)
    sys.exit(0)


if __name__ == "__main__":
    main()
