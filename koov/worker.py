"""Koov worker process — runs one Zoom SDK meeting session in isolation.

Launched as a subprocess by api_server.py with ``KOOV_JOIN_BACKEND=zoom_sdk``.
Status is written to ``<KOOV_NOTES_DIR>/worker-status-<meeting_id>.json``.
"""
from __future__ import annotations

from koov.openai_compat import chat_completions_url as _koov_chat_url, supports_json_mode as _koov_json_ok

import json
import logging
import os
import signal
import sys
import threading
import time


def _write_status(path: str, data: dict) -> None:
    """Atomic write via temp-file rename to avoid partial reads."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass  # best-effort; coordinator falls back to in-memory state


def main() -> None:
    from koov.logging import setup_logging
    setup_logging(os.environ.get("KOOV_LOG_LEVEL", "INFO"))
    log = logging.getLogger(__name__)

    # ── Required config ────────────────────────────────────────────────────────
    meeting_id = os.environ.get("KOOV_MEETING_ID", "").strip()
    meeting_link = os.environ.get("KOOV_MEETING_LINK", "").strip()
    if not meeting_id or not meeting_link:
        sys.stderr.write("[worker] KOOV_MEETING_ID and KOOV_MEETING_LINK must be set\n")
        sys.exit(1)

    display_name = os.environ.get("KOOV_DISPLAY_NAME", "Mechus")
    team_id = os.environ.get("KOOV_TEAM_ID", "").strip()
    shared_context_enabled = os.environ.get("KOOV_SHARED_CONTEXT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    recording_token = os.environ.get("KOOV_RECORDING_TOKEN") or None
    zak_token = os.environ.get("KOOV_ZAK_TOKEN") or None
    participant_timezones_json = os.environ.get("KOOV_PARTICIPANT_TIMEZONES_JSON", "").strip()
    participant_emails_json = os.environ.get("KOOV_PARTICIPANT_EMAILS_JSON", "").strip()
    summary_recipients_json = os.environ.get("KOOV_SUMMARY_RECIPIENTS_JSON", "").strip()
    collection_recipients_json = os.environ.get("KOOV_COLLECTION_RECIPIENTS_JSON", "").strip()
    invite_collection_link = os.environ.get("KOOV_INVITE_EMAIL_COLLECTION_LINK", "").strip()

    # ── Load env + writable notes paths (before status writes) ───────────────
    from koov.envfile import load_env_file
    from koov.notes_paths import apply_notes_paths_to_env

    load_env_file(os.environ.get("KOOV_ENV_FILE", ".env"))
    notes_dir, _store_dir = apply_notes_paths_to_env()
    status_path = os.path.join(notes_dir, f"worker-status-{meeting_id}.json")

    def write(status: str, **extra: object) -> None:
        _write_status(status_path, {
            "status": status,
            "meeting_id": meeting_id,
            "ts": time.time(),
            **extra,
        })

    write("starting")
    log.info("Worker started: meeting_id=%s", meeting_id)

    # ── Config ───────────────────────────────────────────────────────────────
    from koov.facetime_env import apply_zoom_worker_env_defaults

    apply_zoom_worker_env_defaults()
    from koov.config import KoovConfig
    from koov.speaker_map import load_speaker_map_for_meeting, speaker_map_watch_paths

    config = KoovConfig.from_env()

    join_backend = (os.environ.get("KOOV_JOIN_BACKEND") or "zoom_sdk").strip().lower()
    write("joining")
    joined_event = threading.Event()
    join_result: dict = {"done": False, "ok": False, "error": None, "error_code": None}
    result = None

    if join_backend == "zoom_sdk":
        from koov.zoom_sdk_join import parse_zoom_link, create_zoom_session, redact_zoom_link_for_log

        log.info("Worker: Zoom link (redacted): %s", redact_zoom_link_for_log(meeting_link))
        meeting_number, password = parse_zoom_link(meeting_link)
        if meeting_number is None:
            write("error", error="could not parse zoom link")
            log.error("Worker: could not parse zoom link for meeting_id=%s", meeting_id)
            sys.exit(1)

        client_id = os.environ.get("ZOOM_APP_CLIENT_ID", "")
        client_secret = os.environ.get("ZOOM_APP_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            write("error", error="missing ZOOM_APP_CLIENT_ID or ZOOM_APP_CLIENT_SECRET")
            log.error("Worker: missing Zoom credentials for meeting_id=%s", meeting_id)
            sys.exit(1)

        result = create_zoom_session(
            meeting_number=meeting_number,
            password=password,
            display_name=display_name,
            client_id=client_id,
            client_secret=client_secret,
            agent_sample_rate_hz=config.sample_rate_hz,
            agent_frame_ms=config.frame_ms,
            joined_event=joined_event,
            join_result=join_result,
            recording_token=recording_token,
            zak_token=zak_token,
            stream_attribution=config.zoom_stream_attribution,
        )
        if result is None:
            write("error", error="create_zoom_session returned None")
            log.error("Worker: create_zoom_session failed for meeting_id=%s", meeting_id)
            sys.exit(1)
    else:
        write("error", error=f"unknown KOOV_JOIN_BACKEND={join_backend!r}")
        log.error("Worker: unknown join backend for meeting_id=%s", meeting_id)
        sys.exit(1)

    zoom_audio_in, zoom_audio_out, zoom_bot = result

    # Join can block on waiting room or recording permission. Poll join_result so the status
    # endpoint can reflect what's happening (instead of looking "muted"/stuck).
    join_timeout_s = float(os.environ.get("KOOV_ZOOM_JOIN_TIMEOUT_S", "600") or "600")
    t0 = time.monotonic()
    while True:
        if join_result.get("done"):
            break
        if (time.monotonic() - t0) > join_timeout_s:
            write(
                "error",
                error=f"zoom join timed out after {int(join_timeout_s)}s",
                join_state=join_result.get("state"),
                join_detail=join_result.get("detail"),
            )
            log.error(
                "Worker: join timed out after %ss for meeting_id=%s (state=%s detail=%s)",
                join_timeout_s,
                meeting_id,
                join_result.get("state"),
                join_result.get("detail"),
            )
            try:
                zoom_bot.leave()
                zoom_bot.cleanup()
            except Exception:
                pass
            sys.exit(1)
        write("joining", join_state=join_result.get("state"), join_detail=join_result.get("detail"))
        time.sleep(0.25)

    if not join_result.get("ok"):
        err = join_result.get("error") or "zoom join failed"
        write("error", error=err, error_code=join_result.get("error_code"))
        log.error("Worker: zoom join failed for meeting_id=%s: %s", meeting_id, err)
        try:
            zoom_bot.leave()
            zoom_bot.cleanup()
        except Exception:
            pass
        sys.exit(1)

    # ── Create and start agent ─────────────────────────────────────────────────
    from koov.agent import KoovAgent
    agent = KoovAgent(config, audio_in=zoom_audio_in, audio_out=zoom_audio_out)
    # Pin specific past meetings as memory if provided via env var
    _pinned_ids_json = (os.environ.get("KOOV_MEMORY_MEETING_IDS_JSON") or "").strip()
    if _pinned_ids_json:
        try:
            _pinned_ids = json.loads(_pinned_ids_json)
            if isinstance(_pinned_ids, list):
                agent._pinned_memory_ids = [str(m).strip() for m in _pinned_ids if m]
        except Exception:
            pass
    agent.set_meeting(meeting_id)
    if team_id and shared_context_enabled:
        agent.set_team_context(team_id)
    if participant_timezones_json:
        try:
            _tz_map = json.loads(participant_timezones_json)
            if isinstance(_tz_map, dict):
                agent.merge_participant_timezones({str(k): str(v) for k, v in _tz_map.items()})
        except Exception as exc:
            log.warning("Worker participant timezones parse failed: %s", exc)
    participant_emails: dict[str, str] = {}
    if participant_emails_json:
        try:
            _email_map = json.loads(participant_emails_json)
            if isinstance(_email_map, dict):
                participant_emails = {
                    str(k).strip(): str(v).strip().lower()
                    for k, v in _email_map.items()
                    if str(k).strip() and str(v).strip()
                }
        except Exception as exc:
            log.warning("Worker participant emails parse failed: %s", exc)
    summary_recipients: list[str] = []
    if summary_recipients_json:
        try:
            _summary_list = json.loads(summary_recipients_json)
            if isinstance(_summary_list, list):
                summary_recipients = [
                    str(v).strip().lower()
                    for v in _summary_list
                    if str(v).strip() and "@" in str(v)
                ]
        except Exception as exc:
            log.warning("Worker summary recipients parse failed: %s", exc)
    collection_recipients: list[str] = []
    if collection_recipients_json:
        try:
            _collection_list = json.loads(collection_recipients_json)
            if isinstance(_collection_list, list):
                collection_recipients = [
                    str(v).strip().lower()
                    for v in _collection_list
                    if str(v).strip() and "@" in str(v)
                ]
        except Exception as exc:
            log.warning("Worker collection recipients parse failed: %s", exc)
    zoom_audio_in.set_label_override(lambda s, _a=agent: _a._speaker_map.get(s, s))
    _watch_paths = speaker_map_watch_paths(notes_dir, meeting_id)
    _initial = load_speaker_map_for_meeting(notes_dir, meeting_id)
    if _initial:
        try:
            agent.merge_speaker_aliases(_initial)
        except Exception as exc:
            log.debug("Worker initial speaker-map load: %s", exc)
    write("active")
    log.info("Worker active: meeting_id=%s", meeting_id)

    # ── Signal handling ────────────────────────────────────────────────────────
    _stop_event = threading.Event()

    def _on_signal(signum: int, frame: object) -> None:
        log.info("Worker received signal %s for meeting_id=%s; stopping", signum, meeting_id)
        _stop_event.set()
        try:
            agent.stop()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # ── Run agent in thread; heartbeat loop in main ───────────────────────────
    agent_thread = threading.Thread(target=agent.run_forever, daemon=True, name=f"KoovAgent-{meeting_id}")
    agent_thread.start()

    def _speaker_file_poll() -> None:
        last_mtime = 0.0
        while not _stop_event.is_set():
            try:
                mt = 0.0
                for p in _watch_paths:
                    if os.path.isfile(p):
                        mt = max(mt, os.path.getmtime(p))
                if mt > last_mtime:
                    last_mtime = mt
                    m = load_speaker_map_for_meeting(notes_dir, meeting_id)
                    if m:
                        agent.merge_speaker_aliases(m)
            except Exception as exc:
                log.debug("Worker speaker-map poll: %s", exc)
            _stop_event.wait(2.0)

    threading.Thread(target=_speaker_file_poll, daemon=True, name=f"SpeakerMapPoll-{meeting_id}").start()

    transcript_chunks = 0
    while not _stop_event.is_set():
        if not agent_thread.is_alive():
            log.info("Worker: agent thread exited for meeting_id=%s", meeting_id)
            break
        try:
            if hasattr(agent, "_transcript_chunk_id"):
                transcript_chunks = agent._transcript_chunk_id
        except Exception:
            pass
        # Write the note store path so api_server can find the live transcript file
        transcript_path = ""
        try:
            p = agent._note_store.path or ""
            transcript_path = os.path.abspath(p) if p else ""
        except Exception:
            pass
        participants = {}
        attribution_mode = ""
        tz_map = {}
        tz_conflicts = []
        try:
            if hasattr(zoom_bot, "get_participants_snapshot"):
                participants = zoom_bot.get_participants_snapshot() or {}
            if hasattr(zoom_bot, "get_speaker_attribution_mode"):
                attribution_mode = zoom_bot.get_speaker_attribution_mode() or ""
        except Exception:
            pass
        try:
            tz_ctx = agent.get_timezone_context()
            tz_map = tz_ctx.get("participant_timezones") or {}
            tz_conflicts = tz_ctx.get("timezone_conflicts") or []
        except Exception:
            pass
        _thoughts: list = []
        try:
            if hasattr(agent, "get_thoughts_for_status"):
                _thoughts = agent.get_thoughts_for_status()
        except Exception:
            pass
        _live_recap: dict = {}
        try:
            if hasattr(agent, "get_live_recap_for_dashboard"):
                _live_recap = agent.get_live_recap_for_dashboard() or {}
        except Exception:
            pass
        write(
            "active",
            transcript_chunks=transcript_chunks,
            last_activity=time.time(),
            transcript_path=transcript_path,
            team_id=team_id or None,
            shared_context=bool(team_id and shared_context_enabled),
            participants=participants,
            participant_timezones=tz_map,
            timezone_conflicts=tz_conflicts,
            speaker_attribution_mode=attribution_mode,
            thoughts=_thoughts,
            live_recap=_live_recap,
        )
        _stop_event.wait(timeout=5)

    # Agent runs in a separate thread; transcript JSONL is flushed + fsync'd in run_forever's
    # finally block. Wait here so worker-status "stopped" and dashboard GET /api/transcript
    # see the complete file (otherwise race: summary + exit before NoteStore.sync).
    if agent_thread.is_alive():
        log.info("Waiting for agent thread to finish (transcript flush to JSONL)...")
        agent_thread.join(timeout=8.0)
        if agent_thread.is_alive():
            log.warning(
                "Agent thread still running after 8s; proceeding with summary (meeting_id=%s)",
                meeting_id,
            )
    try:
        agent._note_store.sync()
    except Exception:
        pass

    # ── Final meeting summary (for dashboard + email) ─────────────────────────
    transcript_text, state = "", {}
    try:
        transcript_text, state = agent.get_meeting_data_for_summary()
    except Exception as e:
        log.warning("get_meeting_data_for_summary failed: %s", e)

    from koov.meeting_summary import (
        generate_final_summary,
        merge_final_report_into_summary,
        minimal_final_summary_from_transcript_and_state,
        write_final_summary,
    )

    summary = None
    try:
        api_key = os.environ.get("KOOV_OPENAI_API_KEY", "").strip()
        model = (
            getattr(config, "openai_thinking_model", None)
            or getattr(config, "openai_chat_model", None)
            or os.environ.get("KOOV_OPENAI_THINKING_MODEL")
            or os.environ.get("KOOV_OPENAI_CHAT_MODEL", "gpt-4o-mini")
        )
        _shutdown_timeout = min(
            90,
            int(getattr(config, "summary_timeout_seconds", 120) or 90),
        )
        _summary_mode = (getattr(config, "summary_mode", None) or "full").strip().lower()
        if _summary_mode not in ("state", "fast", "full"):
            _summary_mode = "full"
        summary = generate_final_summary(
            transcript_text,
            state,
            api_key=api_key or None,
            model=model,
            mode=_summary_mode,
            max_transcript_chars=config.summary_max_transcript_chars,
            fast_max_tokens=config.summary_fast_max_tokens,
            full_max_tokens=int(os.environ.get("KOOV_SUMMARY_FULL_MAX_TOKENS", "2400") or 2400),
            timeout_seconds=_shutdown_timeout,
            prior_sessions_context=getattr(agent, "_cross_meeting_memory_text", None) or None,
            participant_names=state.get("participant_names"),
        )
        try:
            eng = agent._get_context_engine()
            if eng is not None:
                summary = merge_final_report_into_summary(summary, eng.generate_final_report())
        except Exception as merge_exc:
            log.debug("Final report merge skipped: %s", merge_exc)
        try:
            summary["interactions"] = agent.get_koov_interactions()
        except Exception as ix_exc:
            log.debug("get_koov_interactions failed: %s", ix_exc)
            summary["interactions"] = []
    except Exception as e:
        log.warning("Could not generate final meeting summary: %s", e)
        try:
            if not transcript_text and not state:
                transcript_text, state = agent.get_meeting_data_for_summary()
        except Exception:
            pass
        summary = minimal_final_summary_from_transcript_and_state(transcript_text, state)
        summary["interactions"] = []

    if summary:
        # Auto-generate meeting name BEFORE writing summary so it's included in the response
        try:
            _exec = (summary.get("executive_summary") or "").strip()
            if _exec:
                import urllib.request as _ureq
                import json as _json
                import ssl as _ssl
                _api_key = os.environ.get("KOOV_OPENAI_API_KEY", "").strip()
                if _api_key:
                    _title_model = (
                        getattr(config, "openai_thinking_model", None)
                        or getattr(config, "openai_chat_model", None)
                        or "gpt-4o-mini"
                    )
                    _body = {
                        "model": _title_model,
                        "messages": [
                            {"role": "system", "content": "Generate a short 3-5 word meeting title from this summary. No punctuation, no quotes. Just the title. Examples: 'Q3 Launch Planning', 'Product Roadmap Review', 'Engineering Sprint Kickoff'."},
                            {"role": "user", "content": _exec[:500]},
                        ],
                        "max_tokens": 20,
                        "temperature": 0.3,
                    }
                    _req = _ureq.Request(
                        _koov_chat_url(),
                        data=_json.dumps(_body).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {_api_key}",
                        },
                        method="POST",
                    )
                    try:
                        _ctx = _ssl.create_default_context()
                        with _ureq.urlopen(_req, timeout=10, context=_ctx) as _resp:
                            _data = _json.loads(_resp.read().decode("utf-8"))
                        _name = (_data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip().strip('"').strip("'")
                        if _name:
                            summary["meeting_name"] = _name
                            summary["title"] = _name
                            log.info("Auto-generated meeting name: %s", _name)
                    except Exception as _ne:
                        log.debug("Meeting name generation failed: %s", _ne)
        except Exception as _me:
            log.debug("Meeting name block failed: %s", _me)
        try:
            write_final_summary(
                meeting_id, summary, meeting_link=os.environ.get("KOOV_MEETING_LINK", "")
            )
        except Exception as we:
            log.error("write_final_summary failed: %s", we)
        try:
            from koov.email_sender import send_meeting_emails
            send_meeting_emails(
                summary,
                meeting_id,
                speaker_emails=participant_emails,
                summary_recipients=summary_recipients,
                collection_recipients=collection_recipients,
                invite_collection_link=invite_collection_link,
            )
        except Exception as email_exc:
            log.warning("send_meeting_emails failed: %s", email_exc)

    if agent_thread.is_alive():
        agent_thread.join(timeout=12.0)

    # ── Cleanup ────────────────────────────────────────────────────────────────
    transcript_path_final = ""
    try:
        p = agent._note_store.path or ""
        transcript_path_final = os.path.abspath(p) if p else ""
    except Exception:
        pass
    # Keep transcript_path + meeting_link so GET /api/transcript works after API removes the session.
    _final_thoughts: list = []
    try:
        if hasattr(agent, "get_thoughts_for_status"):
            _final_thoughts = agent.get_thoughts_for_status()
    except Exception:
        pass
    write(
        "stopped",
        transcript_chunks=transcript_chunks,
        transcript_path=transcript_path_final,
        meeting_link=meeting_link,
        team_id=team_id or None,
        shared_context=bool(team_id and shared_context_enabled),
        thoughts=_final_thoughts,
    )
    log.info("Worker stopped: meeting_id=%s", meeting_id)
    try:
        zoom_bot.leave()
    except Exception:
        pass
    try:
        zoom_bot.cleanup()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
