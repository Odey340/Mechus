# Meeting Q&A and Latency

## When someone says "Mechus" during a meeting

1. **Wake word** is detected (e.g. "Mechus").
2. **Request audio** is captured and sent to **STT** (speech-to-text).
3. **Meeting context** is used:
   - **Rolling context**: If the question mentions "summarize", "recap", "action", "what did", etc., the last N seconds of meeting audio are transcribed and added to memory.
   - **Memory**: Topic-based retrieval returns relevant past transcript + recent turns.
4. **Optional web search**: If the user asks for external info (e.g. "What is UGC?", "Look up DNS"), Mechus calls the DuckDuckGo Instant Answer API and adds a short snippet to the prompt. Answers use **meeting context first**, then **web results** if needed.
5. **LLM** (Ollama or rule responder) produces a short, casual answer.
6. **TTS** speaks the response.

No summarization of the whole meeting is done until you explicitly ask (e.g. "Mechus, summarize") or use the context integrity engine’s "END OF MEETING" trigger.

## Current latency (tracked per wake cycle)

| Stage        | Typical range   | Notes                          |
|-------------|------------------|--------------------------------|
| **STT**     | 200–800 ms       | Depends on STT backend         |
| **Context** | 0 or 200–800 ms  | Only when context window used  |
| **Memory**  | 5–20 ms          | In-memory retrieval            |
| **LLM**     | 500–2000 ms      | Ollama/model and prompt size   |
| **TTS**     | 100–500 ms       | Piper (local) or ElevenLabs    |
| **Total**   | ~0.8–3.5 s       | End-to-end wake → speech       |

Latency is **not** tracked for transcript-only processing (continuous transcript chunks without a wake).

## How to read latency

- **API**: `GET /api/status?meeting_id=<id>` returns a `latency` object (when the last wake cycle completed), e.g.:
  ```json
  "latency": {
    "stt_ms": 320.5,
    "context_ms": 0,
    "memory_ms": 12.1,
    "llm_ms": 890.2,
    "tts_ms": 180.4,
    "total_ms": 1402.2
  }
  ```
- **In code**: `agent._last_latency` is updated after each successful wake cycle (and after TTS finishes).

## Transcript at end of meeting

- **Session notes**: Each run writes `mechus_notes/session-YYYYMMDD-HHMMSS.jsonl` with `kind: "transcript"` (meeting chunks) and `kind: "wake_cycle"` (user question + Mechus answer).
- **API**: `GET /api/transcript?meeting_id=<id>` returns:
  - `transcripts`: transcript chunks (with timestamps/speakers when available).
  - `wake_cycles`: each wake Q&A (request + response).
  - `summary`, `actionable_items`: from memory retrieval (if available).

## Testing real-time Q&A and latency

**Script (no microphone):**

```bash
# Uses meeting memory only (no STT/TTS). Measures memory + LLM time.
python3 scripts/test_mechus_meeting_qa.py

# Custom question
python3 scripts/test_mechus_meeting_qa.py --question "What did we decide about the API?"

# Question that triggers web search (needs network)
python3 scripts/test_mechus_meeting_qa.py --web
```

The script prints:

- The **answer** from the responder (meeting context ± web).
- **Latency (ms)** for memory and LLM (no STT/TTS in this script).
- **Transcript at end of meeting**: sample meeting lines + the test Q&A.

For full pipeline latency (including STT and TTS), run a real meeting with Mechus and check `GET /api/status?meeting_id=...` after saying "Mechus" and getting a response.

## Improving efficiency

- **STT**: Use faster-whisper (int8) or Deepgram for lower latency.
- **LLM**: Use a smaller/faster model (e.g. qwen2.5:7b), keep prompts lean (already done), and use response cache for repeated questions.
- **TTS**: Use Piper locally for lower latency; pre-warm if needed.
- **Web search**: Only runs when the question needs external info; DuckDuckGo API is no-key and typically &lt;500 ms.
