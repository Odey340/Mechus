# Koov Personas

Each `*.json` file in this directory defines one Koov bot personality.
Personas coexist in the same meeting — the bot you address is picked by
which wake phrase actually fired (e.g. "hey penny" → Penny, "hey doc"
→ Doc, "koov" → default).

## Adding a voice

ElevenLabs voice IDs are not committed. Reference them with `${VAR}` and
set the variable in `.env` (see `.env.example`):

```
KOOV_PENNY_VOICE_ID=your-elevenlabs-voice-id
KOOV_DOC_VOICE_ID=your-elevenlabs-voice-id
KOOV_CODEY_VOICE_ID=your-elevenlabs-voice-id
KOOV_RIZZO_VOICE_ID=your-elevenlabs-voice-id
```

If a persona's `voice_id` resolves to empty (variable unset), the agent
falls back to `KOOV_ELEVEN_VOICE_ID` (the default Koov voice).

## Schema

```json
{
  "id": "penny",
  "name": "Penny",
  "wake_phrases": ["hey penny", "penny"],
  "voice_id": "${KOOV_PENNY_VOICE_ID}",
  "model_id": null,
  "audio_tags": true,
  "max_words": 55,
  "temperature": 0.4,
  "tools": ["realtime", "stock_quote"],
  "filler_phrases": ["Let me check the tape."],
  "refusal_rules": ["You are not a licensed advisor."],
  "system_prompt": "You are Penny — a sharp finance teammate."
}
```

| Field | Notes |
|------|------|
| `id` | Lowercase identifier, must be unique. |
| `name` | Display name (used inside the system prompt). |
| `wake_phrases` | List of phrases that select this persona. Multi-word wakes ("hey penny") win over single-word ones ("penny"). |
| `voice_id` | ElevenLabs voice ID. `${VAR}` is substituted from env. |
| `model_id` | ElevenLabs model id; null inherits `KOOV_ELEVEN_MODEL_ID`. |
| `audio_tags` | Allow ElevenLabs v3 `[tag]` in spoken output. |
| `max_words` | Soft cap on spoken response length. |
| `temperature` | LLM temperature override (null = use built-in intent curve). |
| `tools` | Whitelisted tools: `realtime`, `stock_quote`, `drug_interactions`, `code_search`. Unknown tools are ignored. |
| `filler_phrases` | Pre-warmed "thinking" sounds spoken in this persona's voice. |
| `refusal_rules` | Plain-English boundaries appended to the system prompt as `Boundaries:`. |
| `system_prompt` | Replaces the built-in Koov persona block. Grounding, priority, audio-tag, and intent rules are still appended automatically. |

## Selecting / disabling personas

`KOOV_PERSONAS_DIR` (default `koov/personas`) — directory to scan.

`KOOV_PERSONAS_ENABLED` — comma-separated allow-list (default = all
files in the directory). The synthetic default Koov persona is always
included so there is always a fallback wake.

## Default persona

There is no `default.json` shipped. The default Koov persona is built
from `KOOV_WAKE_PHRASE`, `KOOV_ELEVEN_VOICE_ID`, and the rest of your
existing config — no behavior change unless you create one.
