# Context Integrity Engine

The **Context Integrity Engine** processes meeting transcripts in chronological chunks and maintains persistent structured memory. It does **not** summarize until it receives the explicit instruction:

```
END OF MEETING — GENERATE OUTPUT
```

## Production-grade behavior

- **Stateful decisions** — Decisions are modeled as state (id, current_value, history, reversed), not flat text. When a decision is reversed, the prior value is superseded and the reversal is logged; "Final Decisions" shows only current state.
- **Topic partitioning** — Facts and decisions are classified into namespaces: `pricing`, `api`, `hiring`, `timeline`, `budget`, `risk`, `action_item`. Enables topic-scoped retrieval so Q&A about "the API" uses only API context.
- **Conflict graph** — Contradictions (e.g. budget 50K vs 40K) are stored as structured conflicts (name, values_mentioned, status) instead of free text.
- **Stricter action items** — Owner must be a known participant; task must be a verb phrase; open questions ("we need to figure out X") are not extracted as action items.
- **Timeline with reversals** — The timeline includes reversal entries (e.g. "Decision api_strategy reversed to v1") so reports and QA reflect supersession.
- **QA API** — For focused retrieval: `get_current_decisions()`, `get_reversal_log()`, `get_conflicts()`, `get_facts_for_topic(topic)`, `get_state_for_qa(topic)` so the LLM receives canonical state + topic-scoped facts instead of unfiltered transcript.

When transcript is enabled, the agent feeds the context engine from each transcript chunk and injects `get_state_for_qa(topic)` and topic-scoped facts into the context passed to the LLM for Q&A.

## Rules

1. **No summarization** until the end-of-meeting trigger.
2. **Structured memory** is built and updated for every chunk.
3. **Contradictions** (e.g. decision reversals), topic shifts, unresolved questions, and reversals are detected.
4. **Speaker-specific positions** are tracked.
5. Tracked entities:
   - Decisions (stateful; reversal = supersession + reversal log)
   - Action items (owner, task, deadline; owner in participants, task = verb phrase)
   - Open questions (not treated as action items)
   - Risks, metrics, assumptions
   - Conflict graph (e.g. budget: 50K vs 40K)

## Per-chunk output

For each chunk you get a **[MEMORY UPDATE]** with:

- New facts
- Updated facts
- Conflicts detected
- Action items added
- Decisions updated
- Open loops remaining

## Final report (after END OF MEETING)

When the trigger is received, the engine produces:

1. Executive Summary (max 300 words)
2. Final Decisions (bullet list)
3. Action Items Table (Owner | Task | Deadline | Origin Timestamp)
4. Contradictions Identified
5. Risks Identified
6. Unresolved Questions
7. Speaker Alignment Matrix
8. Timeline of Key Decision Points
9. Logic Inconsistencies
10. **Confidence score (0–100%)** with reasoning

Reversals (e.g. “we decided X” later “actually we’re not doing X”) are explicitly called out. Ambiguous information is marked **LOW CONFIDENCE**. The engine does not invent missing information.

## Usage

```python
from koov.context_integrity_engine import (
    ContextIntegrityEngine,
    format_memory_update,
    format_final_report,
)

engine = ContextIntegrityEngine(meeting_id="my-meeting")

for chunk_text, speaker_id in chunks:
    mu, is_end = engine.ingest_chunk(chunk_text, speaker_id=speaker_id, timestamp=ts)
    print(format_memory_update(mu))
    if is_end:
        break

report = engine.generate_final_report()
print(format_final_report(report))
```

## Testing

```bash
python3 scripts/test_context_integrity_engine.py
```

The test runs sample meeting chunks (decisions, action items, risks, a reversal, then the end trigger) and prints memory updates and the final report. If confidence is below 70%, it suggests fixes (e.g. clearer “Decision: …”, “Action item: X will Y by Z”, speaker labels, explicit deadlines).

## Improving low confidence

When the confidence score is low, the script suggests:

- Use explicit phrases: “Decision: …”, “We decided …”, “Action item: X will Y by Z”.
- Reduce contradictory statements or add explicit “We are reversing …”.
- Resolve or explicitly defer open questions.
- Use speaker labels (e.g. “Alice:”, “Bob:”) for better owner extraction.
- Include explicit deadlines (“due June 1”, “by next Friday”) for action items.

The engine is **rule-based** (regex and heuristics) so it does not invent content; ambiguous extractions are tagged as low confidence.
