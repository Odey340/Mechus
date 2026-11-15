# Data Privacy

Mechus handles meeting transcripts, voice commands, and meeting links. This document describes how we protect that data and what you should do in production.

## Logging

- **Meeting links** are never logged at INFO. The API server logs only that a link was opened; the full URL is logged only at DEBUG.
- **Transcript chunks** are logged at INFO as chunk ID and character count only. Full transcript text is logged only at DEBUG.
- **User requests and AI responses** (wake-word commands and replies) are logged only at DEBUG.
- **Invalid/short commands** are logged at INFO as the validation reason only; the actual text is logged only at DEBUG.
- **Wake-word / Vosk** partial and final speech text is logged only at DEBUG.

**Recommendation:** Run production with default log level (INFO). Use DEBUG only for local troubleshooting and avoid shipping DEBUG logs to shared systems.

## API Security

The Mechus API (Flask) has no built-in authentication. Anyone who can reach the server can call status, transcript, join, and stop by `meeting_id`.

- **Local use:** Running Mechus and the dashboard on localhost is the typical, low-risk setup.
- **Exposed use:** If the API is reachable beyond localhost (e.g. on a LAN or cloud), put it behind auth (e.g. reverse proxy with API key or SSO, or add an API key in the app). Do not expose it to the public internet without authentication.

## Stored Data

- **Note store:** Transcripts and notes are written to JSONL files under `mechus_notes/` (or the configured root), one file per session: `{session_id}.jsonl`. These files contain meeting content.
  - Restrict filesystem permissions so only the process and trusted admins can read them (e.g. `chmod 600` or equivalent).
  - Plan retention and deletion (e.g. script to delete files older than N days, or per-org policy).
- **No cloud upload by default:** Transcript and audio stay on the machine unless you integrate a third-party STT/TTS or storage service; review those providers’ privacy and data residency.

## Summary

| Data             | INFO log       | DEBUG log  | Storage               |
|------------------|----------------|------------|------------------------|
| Meeting link     | Redacted       | Full URL   | Not stored by Mechus     |
| Transcript text  | Chunk + length | Full text  | `mechus_notes/*.jsonl`   |
| Request/response | —              | Full text  | Not stored by Mechus     |
| Invalid commands | Reason only    | Full text  | —                      |
| Wake-word / Vosk | —              | Full text  | —                      |

Keep log level at INFO in production and protect the `mechus_notes/` directory and API access to preserve data privacy.
