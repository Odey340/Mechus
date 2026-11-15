# Minimal canonical decision state schema

This document locks the **contract** for Koov’s canonical state. The event-extraction layer and the responder (including LLM phrasing) depend on this shape. Test “state completeness” against this schema, not against the LLM.

---

## 1. Purpose

- **Single source of truth** for decisions, action items, open/postponed issues, and reversals.
- **Deterministic correctness**: the LLM only phrases this state; it does not infer structure from transcript.
- **Testability**: we can assert that state contains the right entries (e.g. launch decision, API decision after reversal) without calling the LLM.

---

## 2. Canonical state payload (QA / LLM input)

The object returned by `get_canonical_state_for_qa()` and passed to the responder as `inp.state` has the following shape.

```ts
// Conceptual TypeScript shape (implementation is Python dicts)
interface CanonicalState {
  decisions: DecisionEntry[];
  action_items: ActionItemEntry[];
  open_issues: OpenIssueEntry[];
  reversals: ReversalEntry[];
}

interface DecisionEntry {
  id: string;   // stable key, e.g. "api", "api_strategy", "timeline", "launch_date"
  value: string; // current value only, e.g. "v1", "June 15", "single elimination"
}

interface ActionItemEntry {
  owner: string;
  task: string;
  deadline: string | null;
}

interface OpenIssueEntry {
  topic_key: string;
  status: "open" | "postponed";
  display_summary: string;
}

interface ReversalEntry {
  decision_id: string;
  new_value: string;  // value that superseded the previous one
}
```

- **decisions**: Only **current** decisions (no superseded values). Reversals are listed separately so the LLM can say “we reverted to v1” when relevant.
- **action_items**: Owner, task, deadline. No chunk ids in the contract (used internally for grounding).
- **open_issues**: Both open and postponed; `status` distinguishes. `display_summary` is the human-readable text for “Still open: …” / “Postponed: …”.
- **reversals**: Last N reversals (e.g. 10). Each entry is `decision_id` and the **new** value that replaced the previous one. Used for “we changed from X to Y” phrasing.

---

## 3. Decision ledger (internal state machine)

The **authoritative** decision state lives in the context engine as:

- **DecisionState** per decision id:
  - `id`, `current_value`, `last_updated_chunk`, `origin_ts`
  - `history`: list of (chunk_id, value) for prior values (including reversed)
  - `reversed`: bool
- **Reversal log**: list of (decision_id, from_chunk, new_value, to_chunk).

Rules:

- Only one **current** value per decision id. A reversal updates `current_value` and appends to the reversal log.
- “Provisional” vs “final” is **not** stored as a separate flag; the final value is whatever is in `current_value` after the last reversal/confirmation. Provisional language (“we’ll revisit v2 in Q3”) must **not** overwrite the current decision unless there is an explicit reversal/decision phrase.

---

## 4. Action item ledger

- List of (owner, task, deadline, chunk_id, …). Canonical state exposes only owner, task, deadline.
- No supersession model for action items in the minimal schema (add later if needed).

---

## 5. Open issue registry

- Issues have: topic_key, status (open | resolved | postponed), display_summary, chunk ids for grounding.
- Canonical state exposes only topic_key, status, display_summary for “Still open” / “Postponed”.

---

## 6. Contract for tests

- **State completeness**: After ingesting a script that contains an explicit decision (e.g. “Decision: launch date June 15”), `state.decisions` must contain an entry whose `id` or `value` matches the intended topic (e.g. launch, june, timeline). See `--state-completeness` in the sim.
- **Reversal correctness**: After “revert to v1” / “final decision is v1”, `state.decisions` must have api → v1; `state.reversals` may include the v2 → v1 change. The answer to “What did we decide?” must reflect v1, not v2. See `--failure-case` in the sim.
- **No transcript in LLM prompt when state is present**: Responder uses `_format_state_for_llm(state)` only; no 6k-character transcript dump.

---

## 7. File references

- **Producers**: `koov/context_integrity_engine.py` — `get_canonical_state_for_qa()`, `DecisionState`, `_reversal_log`, `_action_items`, `_issues`.
- **Consumers**: `koov/retrieval.py` (bundle.state), `koov/responder.py` (`_format_state_for_llm`, `_render_state_answer`).
