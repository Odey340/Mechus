# Benchmark Harness and Session-Scoped Memory

## Semantic benchmark

The **benchmark harness** scores **semantic correctness** of Q&A by expected entities/facts per question (not just latency or “response exists”).

### How it works

- Each scenario (default, hard, multi, finance, long) has a spec: `(after_line, question_substring, expected_entities)`.
- After running the simulation, each Q&A pair is matched to a spec by `after_line` and question text containing `question_substring`.
- **Entity score** = (entities that appear in the answer) / (expected entities). Answer is lowercased; entity is a string that must appear (e.g. `"dave"`, `"v1"`, `"june"`).
- **Overall semantic score** = total entities found / total entities expected across all matched questions.

### Usage

```bash
# Single scenario (uses rules responder unless --ollama)
python3 scripts/benchmark_semantic.py --scenario default

# With Ollama for realistic answers
python3 scripts/benchmark_semantic.py --scenario hard --ollama

# Per-question entity breakdown
python3 scripts/benchmark_semantic.py --scenario default --ollama --verbose

# Run all scenarios and print summary table
python3 scripts/benchmark_semantic.py --all --ollama
```

### Exit code

- `0` if overall semantic score ≥ 0.5.
- `1` if below threshold (e.g. rules responder with no LLM).

### Adding or changing expected entities

Edit `scripts/benchmark_semantic.py`: update `BENCHMARK_SPEC` for the scenario. Each entry is `(after_line, question_contains, list_of_expected_entity_strings)`.

---

## Session-scoped memory (meeting_id)

Memory and context engine are **keyed by meeting** so multiple meetings in one process do not cross-bleed.

### Behaviour

- **Agent** keeps:
  - `_memories: dict[meeting_id, ConversationMemory]`
  - `_context_engines: dict[meeting_id, ContextIntegrityEngine]`
  - `_current_meeting_id`
- **`set_meeting(meeting_id)`** switches the current meeting. Transcript and wake cycles use the current meeting’s memory and context engine.
- **`get_memory(meeting_id=None)`** returns the `ConversationMemory` for that meeting (or current if `meeting_id` is None). Used by the API to fetch summary/action items for a specific meeting.
- When a new meeting is joined (e.g. via API), the server calls **`agent.set_meeting(meeting_id)`** so that session uses that meeting’s memory from the first chunk.

### API

- **Join meeting:** Creates agent and calls `agent.set_meeting(meeting_id)` before starting the agent thread. All transcript and Q&A for that meeting use that meeting’s memory.
- **Get transcript/summary:** Uses `session.agent.get_memory(meeting_id)` so the returned summary and action items are for that meeting only.

### No cross-bleed

- Meeting A and Meeting B each have their own `ConversationMemory` and (if transcript enabled) `ContextIntegrityEngine`.
- Switching meetings (e.g. for a different Zoom call) is done by calling `set_meeting(new_meeting_id)`; existing meetings’ data stays in `_memories` and `_context_engines` and is not overwritten.
