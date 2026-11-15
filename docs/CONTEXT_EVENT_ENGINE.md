# Context Event Engine

Production-grade real-time meeting intelligence system that transforms streaming, diarized transcripts into structured, retrievable, grounded knowledge.

## Overview

The Context Event Engine:
1. Continuously ingests streaming transcript segments with speaker diarization
2. Segments speech into semantic, immutable ContextEvents
3. Extracts decisions, action items, claims, and open questions
4. Maintains dual in-memory indices (structured + semantic)
5. Answers questions only with grounded evidence
6. Never speaks unless a wake word is explicitly used
7. Uses Ollama-hosted local LLMs for extraction and answer synthesis

## Core Components

### ContextEvent
Immutable data structure representing a semantic segment of meeting dialogue:
- `event_id`: Unique identifier
- `meeting_id`: Meeting identifier
- `t_start`, `t_end`: Timestamps
- `speaker_ids`: List of speakers
- `raw_text`, `clean_text`: Original and cleaned text
- `topic_id`: Topic identifier
- `event_type`: decision, action_item, question, answer, discussion, risk, meta
- `intent`: propose, agree, disagree, clarify, defer
- `entities`: Extracted entities
- `claims`: Factual claims
- `decisions`: Explicit decisions
- `action_items`: Action items with owner, task, due date
- `open_questions`: Unanswered questions
- `confidence`: Confidence score
- `revoked_by_event_id`: For reversals

### EventBuilder
Streaming state machine that finalizes events based on:
- Topic similarity drops
- Dialogue act changes
- Decision language detection
- Speaker switch + pause >600ms
- Max window size reached

**NO LLM USAGE** - Pure deterministic logic.

### EventExtractor
Tiered extraction strategy:
- **Tier 0** (Always): timestamps, speakers, raw_text, basic entities
- **Tier 1** (Cheap Local): event_type, intent, topic_id
- **Tier 2** (LLM, only if justified): clean_text, explicit decisions, action items, questions

LLM is used ONLY if:
- event_type is decision, action_item, or risk
- OR confidence < threshold
- OR high question density

### InMemoryIndices
Dual indexing system:
- **Structured Index**: decisions by topic, action items by owner, open questions, events by speaker
- **Vector Index**: Semantic embeddings of clean_text, claims, decisions, action_items.task

### QuestionRouter
Deterministic question classification (NO LLM):
- Classifies into: decision, action_item, rationale, recap, fact, speaker_attribution
- Determines time scope: last_2_minutes, topic_scope, whole_meeting
- Routes to appropriate index strategy

### WakeWordDetector
Deterministic wake word detection (NO LLM):
- Primary wake word: "koov" (case-insensitive)
- Activation valid ONLY if:
  1. Segment is is_final = true
  2. Wake word appears with question/command
  3. Usage context directly addresses system
- Enforces cooldown (5-10 seconds)

### AnswerGenerator
Grounded answer synthesis:
- Receives max 3-7 ContextEvents
- LLM instructed to answer ONLY from provided events
- Every answer MUST cite event_ids
- If evidence insufficient → explicitly refuse
- No citations = failed answer

## Usage

### Basic Example

```python
from koov.context_engine import ContextEventEngine

# Initialize engine
engine = ContextEventEngine(
    meeting_id="meeting_123",
    ollama_host="http://127.0.0.1:11434",
    ollama_model="qwen2.5:7b-instruct",
    wake_word="koov",
)

# Ingest streaming transcript segments
event = engine.ingest_segment(
    meeting_id="meeting_123",
    timestamp=1700000000.12,
    speaker_id="spk_1",
    text="Koov what did we decide about pricing",
    is_final=True,
)

# Process wake word
wake_result = engine.process_wake_word(
    segment_text="Koov what did we decide about pricing",
    is_final=True,
)

if wake_result:
    # Answer question
    answer = engine.answer_question(wake_result.cleaned_query)
    print(f"Answer: {answer.answer}")
    print(f"Citations: {answer.event_ids}")
    print(f"Confidence: {answer.confidence}")

# Get metrics
metrics = engine.get_metrics()
print(f"Events/min: {metrics['events_per_minute']}")
print(f"LLM calls/min: {metrics['llm_calls_per_minute']}")
print(f"P95 ingest latency: {metrics['p95_ingest_latency_ms']}ms")
```

### Integration with KoovAgent

The Context Event Engine can be integrated into the existing KoovAgent to replace or augment the current memory and response system.

## Latency Constraints

- Transcript ingest: ≤150ms p95 per segment
- Event finalization: ≤400ms p95
- Question answering: ≤1.2s p95

## Model Usage

- Average ≤1 LLM call per minute of meeting
- LLMs MUST NOT be used for: chunking, routing, wake-word detection, indexing
- LLMs ONLY for: structured extraction, grounded answer synthesis

## Safety & Quality

- Every answer MUST cite event_ids
- If evidence is insufficient → explicitly refuse
- No inferred decisions
- No speculative intent
- Silence is safer than interruption

## Ollama Models

Primary (default): `qwen2.5:7b-instruct`
```bash
ollama pull qwen2.5:7b-instruct
```

Secondary: `llama3.1:8b-instruct`
```bash
ollama pull llama3.1:8b-instruct
```

Optional: `deepseek-r1:7b`
```bash
ollama pull deepseek-r1:7b
```

## Embeddings

The engine uses sentence-transformers for semantic embeddings. Default model: `all-MiniLM-L6-v2` (384 dimensions).

Install:
```bash
pip install sentence-transformers
```

If not available, falls back to simple hash-based vectors (not semantic, but consistent dimensionality).

## Pressure testing

A script stress-tests the context engine with synthetic segments and reports throughput and latency.

**No Ollama (CPU-only):** mocks LLM extraction so you can test event building, embedding, and indices without running Ollama.

```bash
# 30s at 50 seg/s, mock LLM
python scripts/pressure_test_context_engine.py --duration 30 --rate 50 --no-ollama

# Short event windows = more finalizations
python scripts/pressure_test_context_engine.py --duration 20 --rate 30 --no-ollama --min-event 2 --max-event 8

# Burst: feed 1000 segments as fast as possible
python scripts/pressure_test_context_engine.py --burst 1000 --no-ollama --min-event 2 --max-event 6
```

**With Ollama:** run with real extraction and optional `--answers N` to exercise retrieval and answer generation (requires Ollama running).

Output includes: segments fed, events finalized, wall time, throughput (seg/s), events/min, p95 ingest and finalization latency, LLM call count.

## Metrics

Tracked metrics:
- events/minute
- LLM calls/minute
- p95 ingest latency
- p95 answer latency
- refusal rate
- unsupported answer rate

## Replay

The engine supports replay for deterministic event regeneration:

```python
from koov.context_metrics import ReplayEngine

replay = ReplayEngine(engine)
segments = replay.load_transcript_log("transcript.jsonl")
events = replay.replay(segments)
comparison = replay.compare(original_events, events)
```

## Design Decisions

1. **In-memory first**: All indices are in-memory for low latency. Persistence can be added later without refactoring logic.

2. **Deterministic event building**: NO LLM for chunking/routing. Pure state machine logic ensures consistency and low latency.

3. **Tiered extraction**: Most events don't need LLM. Only high-value events (decisions, action items) or low-confidence events trigger LLM.

4. **Strict grounding**: Answers must cite event_ids. No citations = refusal.

5. **Wake word gating**: System never speaks unless explicitly activated via wake word.

6. **Dual indexing**: Structured index for fast lookups, vector index for semantic search.

## Failure Conditions

The system is incorrect if it:
- Embeds raw transcripts (should embed clean_text, claims, decisions, action_items.task)
- Uses LLMs for routing, chunking, or wake detection
- Rewrites past events (events are immutable)
- Speaks without explicit wake word
- Answers without citations

## Future Enhancements

- Persistence layer (PostgreSQL, vector DB)
- Multi-meeting support
- Event revocation tracking
- Advanced topic clustering
- Speaker name resolution
- Real-time streaming API
