# Mechus Personas

Each `*.json` file in this directory defines one Mechus bot personality.
Personas coexist in the same meeting — the bot you address is picked by
which wake phrase actually fired (e.g. "hey penny" → Penny, "hey doc"
→ Doc, "mechus" → default).

## Adding a voice

ElevenLabs voice IDs are not committed. Reference them with `${VAR}` and
set the variable in `.env` (see `.env.example`):

```
MECHUS_PENNY_VOICE_ID=your-elevenlabs-voice-id
MECHUS_DOC_VOICE_ID=your-elevenlabs-voice-id
MECHUS_CODEY_VOICE_ID=your-elevenlabs-voice-id
MECHUS_RIZZO_VOICE_ID=your-elevenlabs-voice-id
```

If a persona's `voice_id` resolves to empty (variable unset), the agent
falls back to `MECHUS_ELEVEN_VOICE_ID` (the default Mechus voice).

## Schema

```json
{
  "id": "penny",
  "name": "Penny",
  "wake_phrases": ["hey penny", "penny"],
  "voice_id": "${MECHUS_PENNY_VOICE_ID}",
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
| `model_id` | ElevenLabs model id; null inherits `MECHUS_ELEVEN_MODEL_ID`. |
| `audio_tags` | Allow ElevenLabs v3 `[tag]` in spoken output. |
| `max_words` | Soft cap on spoken response length. |
| `temperature` | LLM temperature override (null = use built-in intent curve). |
| `tools` | Whitelisted tools: `realtime`, `stock_quote`, `drug_interactions`, `code_search`. Unknown tools are ignored. |
| `filler_phrases` | Pre-warmed "thinking" sounds spoken in this persona's voice. |
| `refusal_rules` | Plain-English boundaries appended to the system prompt as `Boundaries:`. |
| `system_prompt` | Replaces the built-in Mechus persona block. Grounding, priority, audio-tag, and intent rules are still appended automatically. |

## Selecting / disabling personas

`MECHUS_PERSONAS_DIR` (default `mechus/personas`) — directory to scan.

`MECHUS_PERSONAS_ENABLED` — comma-separated allow-list (default = all
files in the directory). The synthetic default Mechus persona is always
included so there is always a fallback wake.

## Default persona

There is no `default.json` shipped. The default Mechus persona is built
from `MECHUS_WAKE_PHRASE`, `MECHUS_ELEVEN_VOICE_ID`, and the rest of your
existing config — no behavior change unless you create one.
