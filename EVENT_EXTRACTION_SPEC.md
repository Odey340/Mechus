# Event-extraction layer spec

This document specifies **what events** the context engine emits from transcript chunks and **how** they update the canonical ledgers (decisions, action items, open/postponed issues, reversals). It is the contract for fixing “state completeness” (e.g. capturing “propose/agree” as decisions) and for avoiding provisional-vs-final blur.

---

## 1. Processing model

- **Input**: One chunk at a time (speaker_id, text, timestamp). Chunks are processed in chronological order.
- **Output**: Updates to in-memory ledgers; no event bus. The only external output is `get_canonical_state_for_qa()` and retrieval tags.
- **Order within a chunk**: (1) Reversal detection and application, (2) conflict detection, (3) action items, (4) new decisions, (5) open questions, (6) issue resolution/postpone.

---

## 2. Event types and ledger updates

### 2.1 Decision proposed / decided

**Intent**: Capture a commitment or explicit decision so it becomes the **current** value for a decision id.

**Current triggers (regex)**:

- `(?:we\s+)(?:have\s+)?(?:decided|agreed|agreed to)\s+[:\s]*([^.;]{10,})`
- `decision\s*[:\-]\s*([^.;\n]{10,})`
- `(?:so\s+)?(?:the\s+)?(?:final\s+)?decision\s+is\s+[:\s]*([^.;]{10,})`
- `let'?s\s+(?:go\s+)?with\s+([^.;]{10,})`

**Ledger update**: `_normalize_decision_id(phrase)` → decision id (e.g. `api`, `timeline`, `launch_date`). If id not in `_decision_states`, create new `DecisionState` with `current_value = phrase`. If id exists, update `current_value` and `last_updated_chunk` (reaffirmation or explicit update). Append to legacy `_decisions` for backward compat.

**Gap (recap error)**: “I propose June 15” / “I agree with June 15” do **not** match the above. So launch date is never written to the decision ledger. **Required extension**: add patterns or a separate “proposal/agreement” path that, when followed by agreement or a second mention, creates or updates a decision (e.g. topic `launch_date` or `timeline`, value “June 15”). Options: (a) pattern like `(?:propose|proposing)\s+([^.;]+)` and `(?:agree|agreed)\s+(?:with\s+)?([^.;]+)` with topic inference; (b) two-phase: “propose X” stores a proposal, “we agree X” or “decision: X” promotes to decision.

---

### 2.2 Decision reversed

**Intent**: When the same decision is explicitly reversed (revert, go back, stay on X, final decision is X), update current value and log the reversal.

**Current triggers**: No regex on chunk text alone. For each existing `DecisionState`, `_detect_negation_reversal(later_text, state.current_value)` is true if:

- Later text contains negation/reversal tokens: `no(?:t)?|never|won't|revert|reverse|instead|actually we|actually ignore`, etc.
- Token overlap between later text and `current_value` is ≥ 0.5 (or ≥ 0.25 when “actually” present).

Then `_extract_reversal_value(reversal_text, old_value)` extracts the new value from phrases like “revert to v1”, “stay on v1”, “final decision is v1”.

**Ledger update**: Append `(decision_id, from_chunk, new_value, to_chunk)` to `_reversal_log`. Update `DecisionState`: `current_value = new_value`, `history.append((last_updated_chunk, old_value))`, `reversed = True`, `last_updated_chunk = cid`. Mark legacy `_decisions` entry as `reversed_by_chunk_id = cid` and append new record for `new_value`.

**Provisional vs final**: Phrases like “We’ll revisit v2 in Q3” or “We’re aligned on v2 directionally” must **not** overwrite current value. They are not reversals (no negation of current value) and should not match “new decision” patterns that set current to v2. Current implementation is safe: those phrases don’t match `_DECISION_PATTERNS` (no “decision:” or “final decision is”), and reversal only fires when negation + overlap with **current** value. So “revisit v2 in Q3” does not overwrite v1. The failure-case test encodes this: after “final decision is v1”, state must remain api → v1.

---

### 2.3 Action item added

**Intent**: Capture owner, task, and optional deadline.

**Current triggers**: `_extract_action_items(text, speaker_id, …)` using patterns such as:

- `action\s*(?:item)?\s*[:\-]?\s*([A-Za-z][A-Za-z\s]{1,30}?)\s+will\s+([^.;]+?)(?:\s+by\s+([^.;]+?))?(?:\.|;|$)`
- `\b([A-Z][a-z]+)\s+(?:will|to|should|has to)\s+([^.;]{10,}?)(?:\.|;|$)`
- `(?:I'll|I will)\s+([^.;]{10,}?)(?:\s+by\s+([^.;]+?))?(?:\.|;|$)`

**Ledger update**: Dedupe by (owner_canon, task_root). Append to `_action_items`. Optionally resolve an open issue when the action answers “who owns X” (e.g. “Carol will own marketing” closes “who owns marketing?”).

---

### 2.4 Open question / issue raised

**Intent**: Track open and postponed issues for “Still open” / “Postponed” in canonical state.

**Current triggers**: `_extract_open_questions(text)` (e.g. “Open question: …”, “we need to figure out …”, sentence ending in `?`). Then `_issue_topic_key(question_text)` → topic_key; `_issue_display_summary(topic_key, question_text)` → display_summary.

**Ledger update**: One open issue per topic_key. Append to `_issues` with status `open`. Later, `_apply_issue_resolution` can set status to `resolved` or `postponed` based on rules (e.g. “we’re postponing X”, “X is resolved”).

---

### 2.5 Issue resolved or postponed

**Intent**: Close or postpone an open issue when the transcript says so.

**Current triggers**: Implemented in `_apply_issue_resolution(chunk_text, chunk_id)` (e.g. keywords for “postpone”, “resolve”, or resolution by action-item ownership).

**Ledger update**: Set `issue.status` to `resolved` or `postponed`, set `resolved_chunk_id` and evidence as needed.

---

## 3. What to add for “state completeness”

1. **Launch / timeline decision**: Ensure a decision with id or value mentioning “launch”, “june”, or “timeline” is present when the transcript contains an explicit decision like “Decision: we have decided the launch date is June 15.” Current patterns already match that sentence; the gap is scripts that only have “I propose June 15” / “I agree with June 15” with no “Decision:” line. So either:
   - **Option A**: Add patterns for “(we )?propose …” / “(we )?agree …” and map to a decision id (e.g. timeline/launch_date) and value (e.g. “June 15”), or
   - **Option B**: Add a two-phase “proposal” store and promote to decision on “agree” or “decision:” in a later chunk.

2. **Provisional vs final**: Do **not** add patterns that treat “we’ll revisit X” or “aligned on X directionally” as updating current decision. Keep reversal and “new decision” logic as above; run `--failure-case` to regress.

---

## 4. File references

- **Extraction**: `koov/context_integrity_engine.py` — `_extract_decisions`, `_extract_reversal_value`, `_detect_negation_reversal`, `_extract_action_items`, `_extract_open_questions`, `_apply_issue_resolution`, `_DECISION_PATTERNS`, `_ACTION_PATTERNS`, `_QUESTION_PATTERNS`.
- **Ledgers**: same file — `_decision_states`, `_reversal_log`, `_action_items`, `_issues`; `ingest_chunk` applies extraction and updates.
- **Contract**: `CANONICAL_STATE_SCHEMA.md`; tests: `scripts/simulate_meeting_test.py` (`--failure-case`, `--state-completeness`).
