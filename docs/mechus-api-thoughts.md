# Mechus API: Thoughts / Reasoning Trace

This document describes the canonical `thoughts[]` field on `GET /api/status`,
added to expose Mechus's internal reasoning to the mechus-app Thoughts column.

## Endpoint

```
GET /api/status?meeting_id=<uuid>
GET /api/status?session_group_id=<uuid>    (multi-bot)
```

Polled by the dashboard BFF (`/api/mechus/status`) every ~1.8 s during active meetings.

---

## Canonical `thoughts[]` shape

```json
{
  "ts": 1710000004,
  "kind": "thinking",
  "text": "User asked about audio; no prior failures in this session.",
  "planned_speech": "Yes, I can hear you clearly.",
  "connections": ["mic check at 14:02"]
}
```

### Fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `ts` | `number` | yes | Unix **seconds** (integer). mechus-app normalises ms if > 1e12. |
| `kind` | `string` | yes | One of: `thinking`, `connection`, `planned`, `context` |
| `text` | `string` | yes | Main reasoning body (never spoken aloud) |
| `planned_speech` | `string` | no | What Mechus is about to say aloud |
| `connections` | `string[]` | no | Short labels for prior-context links |

Optional fields are **omitted** (not `null`) when absent.

### Kind semantics

| Kind | When emitted | Content |
|------|--------------|---------|
| `thinking` | Wake detected, intent classified, LLM `private_thoughts` | Internal reasoning step |
| `connection` | When retrieval links strong prior context | Populate `connections[]` |
| `planned` | Just before Mechus speaks | `planned_speech` = exact upcoming utterance |
| `context` | After retrieval bundle is ready | Summary of retrieved context |

---

## Single-bot status response

```json
{
  "meeting_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "active",
  "active": true,
  "live_transcripts": [
    { "timestamp": 1710000003, "speaker": "User", "text": "Can you hear me?" }
  ],
  "thoughts": [
    {
      "ts": 1710000002,
      "kind": "thinking",
      "text": "Heard: Mechus can you hear me"
    },
    {
      "ts": 1710000003,
      "kind": "thinking",
      "text": "Intent: presence_check — can you hear me"
    },
    {
      "ts": 1710000003,
      "kind": "context",
      "text": "Retrieved 480 chars of context for: can you hear me",
      "connections": ["audio setup at 09:14"]
    },
    {
      "ts": 1710000004,
      "kind": "thinking",
      "text": "User wants audio confirmation; session has no prior failures."
    },
    {
      "ts": 1710000004,
      "kind": "planned",
      "text": "Responding to: can you hear me",
      "planned_speech": "Yes, I can hear you clearly."
    }
  ]
}
```

---

## Multi-bot (`session_group_id`) response

Each bot in `bots[]` includes its own `thoughts[]` and `live_transcripts[]` (Option A):

```json
{
  "active": true,
  "session_group_id": "abc123",
  "bots": [
    {
      "bot_meeting_id": "bot-uuid-1",
      "persona_id": "default",
      "display_name": "Mechus",
      "status": "active",
      "live_transcripts": [ ... ],
      "thoughts": [
        { "ts": 1710000004, "kind": "thinking", "text": "..." }
      ]
    },
    {
      "bot_meeting_id": "bot-uuid-2",
      "persona_id": "penny",
      "display_name": "Penny",
      "status": "active",
      "live_transcripts": [ ... ],
      "thoughts": [ ... ]
    }
  ]
}
```

mechus-app merges `bots[].thoughts`, tagging each row with `display_name` or `persona_id` when present.

---

## `GET /api/transcript` (ended meetings)

After a meeting ends, `thoughts` is also returned on the transcript endpoint, populated
from persisted `kind="thought"` entries in the session JSONL:

```json
{
  "success": true,
  "meeting_id": "...",
  "transcripts": [ ... ],
  "wake_cycles": [ ... ],
  "thoughts": [ ... ],
  "session_ended": true
}
```

---

## Live behaviour

- **Append-only** during the meeting: entries accumulate with increasing `ts`. The stream is capped at **80** entries (oldest dropped).
- **Stable deduplification**: same `ts` second + same `text` prefix → skipped on re-poll.
- `thoughts` is present on **every** status poll while `active: true` (returns `[]` if none yet).
- When the meeting ends, the last snapshot is written to the worker-status file and JSONL.

---

## Enabling `private_thoughts` (model reasoning)

Mechus emits heuristic thoughts (wake detection, retrieval context, planned speech) without
any special config. For the highest-fidelity `thinking` entries (the model's own one-sentence
reasoning before speaking), enable JSON mode:

```env
MECHUS_RESPONSE_JSON_MODE=1
```

This instructs the LLM to emit `private_thoughts` alongside `spoken_response`, which are
captured and surfaced as `kind: "thinking"` entries.

Recommended in the FaceTime worker env bundle (set in `_facetime_worker_docker_env_flags`
in `mechus/api_server.py`).

---

## Consumer reference

mechus-app consumer: `lib/mechus-thoughts.ts` → `extractMechusThoughtsFromMechusStatus`.

The dashboard polls `MeetingDetailClient → /api/mechus/status → Mechus GET /api/status`.

**Canonical keys** (lock `lib/mechus-thoughts.ts` to these; temporary aliases can be removed):

- `thoughts` (top-level for single-bot; `bots[].thoughts` for multi-bot)
- Per-entry: `ts`, `kind`, `text`, `planned_speech`, `connections`
