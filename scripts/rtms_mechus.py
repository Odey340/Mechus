#!/usr/bin/env python3
"""
Mechus with Zoom RTMS + Meeting SDK hybrid (Zoom only).

When a user starts a meeting with your Zoom App, Zoom sends meeting.rtms_started.
We connect to RTMS for audio in (real-time, no recording prompt), and optionally
join the same meeting via Meeting SDK for audio out (TTS). Bot joins muted and
only unmutes when Mechus responds (wake word, "Mechus", or question).

Requirements:
  - Zoom App (General App) with RTMS entitlements
  - Meeting SDK app (for TTS) – separate app for joining
  - RTMS enabled for your Zoom account
  - pip install rtms python-dotenv

Usage:
  1. Set ZM_RTMS_CLIENT and ZM_RTMS_SECRET in .env (Zoom App for RTMS)
  2. Set ZOOM_APP_CLIENT_ID and ZOOM_APP_CLIENT_SECRET (Meeting SDK app for TTS)
  3. Set ZM_MEETING_NUMBER and ZM_MEETING_PASSWORD when same meeting (or from payload)
  4. Run: python scripts/rtms_mechus.py
  5. Configure Zoom App webhook URL to https://your-server:PORT/webhook

Without Meeting SDK (e.g. on macOS): RTMS-only, responses to logs.
"""
from __future__ import annotations

import os
import sys
import threading
import time

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _meeting_info_from_payload(pl: dict) -> tuple[int | None, str | None]:
    """Extract (meeting_number, password) from RTMS webhook payload, store, or env."""
    num = None
    pwd = None
    for key in ("meeting_id", "meeting_number", "id"):
        v = pl.get(key)
        if v is not None:
            try:
                num = int(v)
                break
            except (TypeError, ValueError):
                pass
    for key in ("password", "pwd"):
        v = pl.get(key)
        if v is not None and isinstance(v, str) and v.strip():
            pwd = v.strip()
            break
    if num is None:
        try:
            from mechus.rtms_meeting_store import get_meeting
            stored = get_meeting()
            if stored:
                num, pwd = stored
                print("[RTMS] Using meeting from dashboard (set-meeting-for-rtms)", flush=True)
        except Exception:
            pass
    if num is None:
        mn = os.getenv("ZM_MEETING_NUMBER") or os.getenv("ZOOM_SDK_MEETING_NUMBER")
        pw = os.getenv("ZM_MEETING_PASSWORD") or os.getenv("ZOOM_SDK_MEETING_PASSWORD")
        if mn:
            try:
                num = int(mn)
                pwd = pw.strip() if (pw and isinstance(pw, str)) else None
            except (TypeError, ValueError):
                pass
    return num, pwd


def _make_audio_callback(audio_in, agent_sample_rate_hz, rtms_sample_rate_hz):
    """Build onAudioData callback that pushes resampled frames to audio_in."""
    from mechus.dsp import pcm16_resample_linear

    frame_bytes_agent = int(agent_sample_rate_hz * 0.02 * 2)  # 20ms frames

    def on_audio(data, size, timestamp, metadata):
        if data is None:
            return
        if hasattr(data, "tobytes"):
            buf = data.tobytes()
        else:
            buf = bytes(data)[: int(size)] if size else bytes(data)
        if not buf:
            return
        if rtms_sample_rate_hz != agent_sample_rate_hz:
            buf = pcm16_resample_linear(buf, rtms_sample_rate_hz, agent_sample_rate_hz)
        ts_s = float(timestamp) / 1000.0 if timestamp else time.monotonic()
        offset = 0
        while offset + frame_bytes_agent <= len(buf):
            chunk = buf[offset : offset + frame_bytes_agent]
            offset += frame_bytes_agent
            audio_in.push_frame(chunk, ts_s)

    return on_audio


def main():
    from mechus.agent import MechusAgent
    from mechus.config import MechusConfig
    from mechus.envfile import load_env_file
    from mechus.rtms_input import RTMSAudioInput, rtms_available

    if not rtms_available():
        print("Error: rtms package required. Run: pip install rtms")
        sys.exit(1)

    import rtms

    load_env_file(os.getenv("MECHUS_ENV_FILE", ".env"))
    config = MechusConfig.from_env()

    # RTMS typically 16kHz; Mechus uses config.sample_rate_hz (often 48k)
    agent_sr = config.sample_rate_hz
    rtms_sr = 16000

    # Null output when no Meeting SDK (RTMS-only)
    class NullAudioOutput:
        def play(self, *a, **k):
            pass

        def play_blocking(self, *a, **k):
            pass

        def wait(self):
            pass

        def stop(self):
            pass

    active_clients = {}
    client_lock = threading.Lock()

    def _make_audio_out(pl):
        """Build audio output: Meeting SDK (on-demand mute) if available, else null."""
        from mechus.zoom_sdk_join import (
            create_zoom_session,
            OnDemandMuteZoomOutput,
            zoom_sdk_available,
        )
        meeting_number, password = _meeting_info_from_payload(pl)
        client_id = os.getenv("ZOOM_APP_CLIENT_ID")
        client_secret = os.getenv("ZOOM_APP_CLIENT_SECRET")
        if (
            zoom_sdk_available()
            and meeting_number is not None
            and client_id
            and client_secret
        ):
            joined = threading.Event()
            result = create_zoom_session(
                meeting_number=meeting_number,
                password=password,
                display_name="Mechus",
                client_id=client_id,
                client_secret=client_secret,
                agent_sample_rate_hz=agent_sr,
                agent_frame_ms=config.frame_ms,
                joined_event=joined,
                join_muted=True,
            )
            if result:
                _, zoom_audio_out, zoom_bot = result
                if joined.wait(timeout=120):
                    print("[RTMS+Zoom] Bot joined for TTS; will unmute only when responding", flush=True)
                    return OnDemandMuteZoomOutput(zoom_audio_out, zoom_bot), zoom_bot
                zoom_bot.leave()
                zoom_bot.cleanup()
        return NullAudioOutput(), None

    @rtms.onWebhookEvent(port=int(os.getenv("ZM_RTMS_PORT", "8080")), path=os.getenv("ZM_RTMS_PATH", "/webhook"))
    def handle_webhook(payload):
        event = payload.get("event", "")
        print(f"[RTMS] Webhook received: event={event!r}", flush=True)
        if event not in ("meeting.rtms_started", "meeting.rtms.started"):
            print(f"[RTMS] Ignored (expected meeting.rtms_started or meeting.rtms.started)", flush=True)
            return

        pl = payload.get("payload", {})
        stream_id = pl.get("rtms_stream_id")
        if not stream_id:
            print("[RTMS] Webhook payload missing rtms_stream_id; keys: %s" % list(pl.keys()), flush=True)
            return

        audio_in = RTMSAudioInput(agent_sr, config.frame_ms)
        audio_in.start()

        audio_out, zoom_bot = _make_audio_out(pl)

        client = rtms.Client()
        client.enableAudio(True)
        client.enableTranscript(True)

        cb = _make_audio_callback(audio_in, agent_sr, rtms_sr)
        client.onAudioData(cb)

        agent = MechusAgent(config, audio_in=audio_in, audio_out=audio_out)
        agent.set_meeting(stream_id)

        def on_join(reason):
            print(f"[RTMS] Joined stream {stream_id}: {reason}", flush=True)

        def on_leave(reason):
            print(f"[RTMS] Left stream {stream_id}: {reason}", flush=True)
            agent.stop()
            audio_in.stop()
            if zoom_bot:
                try:
                    zoom_bot.leave()
                    zoom_bot.cleanup()
                except Exception as e:
                    print(f"[RTMS] Zoom bot cleanup: {e}", flush=True)
            with client_lock:
                active_clients.pop(stream_id, None)

        client.onJoinConfirm(on_join)
        client.onLeave(on_leave)

        meeting_uuid = pl.get("meeting_uuid")
        server_urls = pl.get("server_urls")
        signature = pl.get("signature")
        if not meeting_uuid or not server_urls or not signature:
            print(f"[RTMS] Payload missing required fields (meeting_uuid=%s server_urls=%s signature=%s)"
                  % (bool(meeting_uuid), bool(server_urls), bool(signature)), flush=True)
            return
        ok = client.join(
            meeting_uuid=meeting_uuid,
            rtms_stream_id=stream_id,
            server_urls=server_urls,
            signature=signature,
        )
        if not ok:
            print(f"[RTMS] Join failed for {stream_id} (check ZM_RTMS_CLIENT, ZM_RTMS_SECRET, RTMS enabled for account)", flush=True)
            if zoom_bot:
                zoom_bot.leave()
                zoom_bot.cleanup()
            return

        with client_lock:
            active_clients[stream_id] = {"client": client, "agent": agent}

        def run_agent():
            try:
                agent.run_forever()
            except Exception as e:
                print(f"[RTMS] Agent error for {stream_id}: {e}", flush=True)
            finally:
                audio_in.stop()

        t = threading.Thread(target=run_agent, daemon=True, name=f"Mechus-{stream_id}")
        t.start()

    port = int(os.getenv("ZM_RTMS_PORT", "8080"))
    path = os.getenv("ZM_RTMS_PATH", "/webhook")
    print(f"[RTMS] Mechus listening for Zoom webhooks on port {port}, path {path}", flush=True)
    print("[RTMS] Configure your Zoom App webhook URL to point here.", flush=True)
    rtms.run(poll_interval=0.01)


if __name__ == "__main__":
    main()
