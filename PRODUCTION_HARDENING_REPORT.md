# Production Hardening Pass — Report (post pressure-test)

## Verification summary (scripts/verify_hardening.py)

| Step | Check | Result |
|------|--------|--------|
| A | Total LLM calls, P95, % with citations | 0 LLM calls, P95 0.04 ms, 100% citations |
| B | Action items answer has only tasks, not decisions | PASS |
| C | Missing evidence (partnerships) → refuse | PASS |
| D | No invented due date when none in event | PASS |
| E | "Mechus is cool" → no activation | PASS |
| F | event_ids present on structured path | PASS |

Latency is reported with **2 decimal places** (float ms); structured path shows ~0.03–0.04 ms (no longer 0.0).

---

## 1. Files modified

| File | Change |
|------|--------|
| **mechus/json_utils.py** | `safe_parse_json(text)`; `extract_structured_event(text, meta)` for extraction from text; `get_structured_qa_state(engine)` for retrieval (event_ids from engine). |
| **mechus/context_integrity_engine.py** | Added `get_qa_event_ids()` and `extract_from_chunk(text, speaker_id, chunk_id, participants)` for text-only extraction. |
| **mechus/structured_response.py** | Tier 1 + Tier 2: returns `(answer, event_ids)`. Uses `get_structured_qa_state(engine)`. Tier 2 template recap when intent is recap and structured data exists. |
| **mechus/intent.py** | Added `INTENT_PERSON_ACTION`, `INTENT_RATIONALE`. Rules-first mapping (person action, why → rationale). |
| **mechus/responder.py** | Intent-based rules; `safe_parse_json` for Ollama response; reduced LLM payload (structured + 2k context); timeout 1.5s; max_predict=120; strict grounding instruction. |
| **mechus/agent.py** | Tiered path uses `(answer, event_ids)`; grounding: refuse when `event_ids` empty on LLM path; latency: `router_ms`, `memory_ms`, `llm_ms`, `total_ms`. |
| **mechus/config.py** | `wake_cooldown_s` default 2.5 → **7.0**. |
| **mechus/wake.py** | Vosk: fire only on final; `_has_command_or_question(text)` so "Mechus is cool" does not activate. `_json_text` logs on parse failure. |
| **mechus/api_server.py** | `safe_parse_json` for transcript/note JSON; no silent swallow of parse errors. |
| **scripts/simulate_meeting_test.py** | Tuple `(answer, event_ids)`; latency with 2 decimals; `llm_called`, `event_ids`, `structured_path` per Q&A; summary: P95, total LLM calls, % with citations. |
| **scripts/verify_hardening.py** | **NEW**. Steps A–F: hard metrics, action-items-only, missing-evidence refuse, no invented due date, wake negative, event_ids on structured path. |

---

## 2. Explanation of each change

### 1) Extraction vs retrieval (no engine-coupled extraction)
- **`extract_structured_event(text, meta)`** in `json_utils.py`: extraction from text only; `event_ids` from `meta["chunk_id"]`. Used when processing a single chunk. No engine.
- **`get_structured_qa_state(engine)`** in `json_utils.py`: retrieval from engine; returns current state and `event_ids` from `engine.get_qa_event_ids()`. Tier 1/2 use this.
- **`context_integrity_engine.extract_from_chunk(text, speaker_id, chunk_id, participants)`**: pure extraction from one chunk; returns (action_items, decisions, open_questions). Called by `extract_structured_event(text, meta)` and by ingest internally.

### 2) Safe JSON parsing
- **`safe_parse_json(text)`**: tries `json.loads`; on failure extracts first `{...}` or `[...]` with regex; on failure returns `{_parse_failed: True, _parse_error: str}`. Used in responder (Ollama body) and api_server (transcript lines). Pipeline never crashes on bad JSON.

### 3) Tiered responder
- **Tier 1**: action_item, decision, open_question → structured memory + template; no LLM; target &lt;50 ms (achieved ~0.03–0.04 ms in sim with 2-decimal instrumentation).
- **Tier 2**: recap when structured data exists → template recap from decisions + action items + open questions; no LLM.
- **Tier 3**: LLM only when Tier 1/2 don’t apply (e.g. rationale, ambiguous). Constraints: max 120 tokens, temperature 0.1, timeout 1.5 s, strict grounding.

### 4) Grounding contract
- Before returning: if `event_ids` empty and answer is from LLM path (not Tier 1/2), response is replaced with: *"I don't have enough information yet."*
- Tier 1/2 always have `event_ids` from `get_structured_qa_state(engine)` (which uses `engine.get_qa_event_ids()`).

### 5) Router (rules-first)
- Intent remains in `intent.py`: explicit rules for action_item, decision, recap, person_action, rationale. No semantic routing in main path; `classify_intent` is deterministic.

### 6) Wake word
- **Vosk**: only fires on **final** hypothesis (`is_final`); partial hypothesis path removed.
- **Cooldown**: default **7 s** (`MECHUS_WAKE_COOLDOWN_S`).
- No user-facing “wake word not detected” message; when not activated the agent returns `None` and does not speak.

### 7) Reduce LLM payload
- Context to LLM: structured block (canonical state, action items, open questions) + at most 2500 chars structured + 1500 chars recent; memory trimmed to 2000 chars. No full transcript or full meeting history.

### 8) Optional fast embedding
- Not implemented. System works without it. Can be added later with sentence-transformers and cosine retrieval (top 5) without blocking.

### 9) Bug fixes
- **api_server**: JSON parsing uses `safe_parse_json`; parse failures logged/skipped explicitly; exception handling limited to `KeyError`, `IndexError`, `TypeError` (no silent broad `Exception` for parse).
- **wake.py**: `_json_text` catches `Exception` and logs at debug instead of bare except.

### 10) Latency metrics
- **Agent**: `_last_latency` uses **float ms rounded to 2 decimals** (so structured path shows ~0.01–0.04 ms, not 0.0). Includes `router_ms`, `memory_ms`, `llm_ms`, `total_ms`.
- **Simulation**: each Q&A has `router_ms`, `memory_ms`, `llm_ms`, `total_ms`, `llm_called`, `event_ids`, `structured_path`. Summary prints: P95 latency, total LLM calls, % answers with event_ids (citations).

---

## 3. Confirmation

| Requirement | Status |
|-------------|--------|
| Structured routing active | Yes. `classify_intent` rules-first; Tier 1/2 use structured only. |
| Fast path working | Yes. Action items, decisions, open questions, recap (template) use no LLM; sim shows 0 ms for these. |
| LLM calls reduced | Yes. Recap uses Tier 2 template when structured data exists; LLM only for rationale/ambiguous. |
| Grounding enforced | Yes. Empty `event_ids` on LLM path triggers refusal. Tier 1/2 always have `event_ids`. |

---

## 4. Updated latency metrics (simulation)

- **Structured path**: ~0.03–0.04 ms total (router + retrieval + format); reported with 2 decimals.
- **Total LLM calls**: 0 / 4 for default sim (all Tier 1/2).
- **% with citations (event_ids)**: 100%.
- **P95 latency**: 0.04 ms (structured). With Ollama, Tier 3 would add llm_ms (target P95 &lt; 1500 ms).

---

## 5. Verdict

**SAFE TO APPLY**

- No breaking refactors; wake-word gating kept; no new product features.
- Structured queries use fast path; recap uses template when possible; grounding and safe JSON in place; wake strict (final-only, 7 s cooldown); latency metrics added.
