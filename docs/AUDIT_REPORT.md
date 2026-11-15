# Production Safety Audit Report
**Date:** 2026-01-27  
**Scope:** Current Cursor worktree  
**Auditor Role:** Principal Software Engineer & Systems Auditor

---

## ✅ PASSED CHECKS

### Voice Output Gating (Partial Pass)
**File:** `mechus/agent.py`  
**Lines:** 1016, 1048

- ✅ TTS synthesis (`self._tts.synthesize()`) is only called within `_run_wake_cycle()` method
- ✅ Both call sites occur after wake word detection (line 990: `if self._wake.process(...)`)
- ✅ TTS is gated by wake word activation in the main execution flow
- ⚠️ **Note:** Background preprocessor generates responses but does NOT call TTS (acceptable)

**Evidence:**
- Line 990: Wake detection gate
- Lines 1004-1010: Wake cycle execution
- Lines 1016, 1048: TTS calls only after wake cycle completes

---

## ⚠️ WARNINGS (NON-BLOCKING)

### 1. Background Preprocessor Generates Responses Without Wake Word
**File:** `mechus/preprocessor.py`  
**Lines:** 91-133

The `BackgroundPreprocessor` generates responses in the background for common questions. While it does NOT call TTS (which is correct), it does call the responder LLM without a wake word activation.

**Impact:** Low - responses are cached but not spoken. However, this violates the principle that LLM should only be used after wake word activation.

**Recommendation:** Consider disabling background preprocessor or ensuring it only runs after recent wake word activations.

---

### 2. Transcript Refinement Uses LLM
**File:** `mechus/transcript.py`  
**Lines:** 273-328

`TranscriptRefiner` uses an LLM to clean transcripts. This is acceptable as "Tier-2 extraction" but should be documented as such.

**Status:** Acceptable for now, but should be explicitly categorized.

---

## ❌ FAILURES (BLOCKING)

### 1. Wake-Word Safety Violations (CRITICAL)

**File:** `mechus/wake.py`  
**Lines:** 51-196

#### Violation 1.1: Non-Deterministic Wake Word Detection
- **Line 60:** Uses Vosk ASR model (`Model(model_path)`) - a neural network, NOT deterministic
- **Line 149:** Uses openWakeWord model - also a neural network, NOT deterministic
- **Requirement:** Wake word detection must be "purely deterministic" with "regex / string matching only"

**Evidence:**
```python
# Line 60: Vosk model (neural network)
self._model = Model(model_path)
self._recognizer = KaldiRecognizer(self._model, self._sr, grammar)

# Line 149: openWakeWord model (neural network)
self._model = Model(wakeword_models=[model_path])
```

#### Violation 1.2: Wake Word Checked on Partial Results
- **Lines 98-111:** `VoskWakeDetector.process()` checks partial results (`is_final == False`)
- **Requirement:** Wake word must be checked ONLY on `is_final == true` transcript segments

**Evidence:**
```python
# Line 98: Checks partial results
is_final = self._recognizer.AcceptWaveform(pcm16)
if not is_final:
    # Uses partial hypotheses - VIOLATION
    partial_text = self._json_text(self._recognizer.PartialResult(), key="partial")
    if self._contains_phrase(partial_text):
        # ... fires on partial
```

#### Violation 1.3: No Co-Occurrence Check with Question/Command
- **Missing:** Wake word detection does not verify that wake word co-occurs with a question or command
- **Requirement:** "Wake word must co-occur with a question or command"
- **Current:** Wake word can fire on abstract mentions (e.g., "someone mentioned mechus earlier")

**Evidence:** No code exists to validate that the wake phrase appears alongside actionable patterns.

#### Violation 1.4: No Structured Activation Result
- **Current:** `process()` returns only `bool`
- **Requirement:** Must return structured result with:
  - `activated: bool`
  - `confidence: float`
  - `cleaned_query: str`

**Evidence:**
```python
# Line 93: Returns only bool
def process(self, pcm16: bytes, timestamp_s: float) -> bool:
```

#### Partial Pass: Cooldown Enforcement
- ✅ **Line 64, 141:** Cooldown gate is implemented and enforced
- ✅ **Lines 100, 120, 192:** Cooldown checks occur before activation

---

### 2. ContextEvent Immutability Violations (CRITICAL)

**File:** `mechus/memory.py`  
**Lines:** 13-29, 121-162

#### Violation 2.1: No ContextEvent Class Exists
- **Missing:** The codebase has no `ContextEvent` class
- **Requirement:** System must use `ContextEvent` objects that are append-only

#### Violation 2.2: Mutable Event Storage
- **Line 18:** `TopicContext.segments` is a mutable list
- **Line 22-24:** `add_segment()` mutates the segments list
- **Line 142:** `ConversationMemory._segments` is a mutable list
- **Requirement:** "No function mutates an existing event"

**Evidence:**
```python
# Line 18: Mutable list
segments: list[tuple[float, str]] = field(default_factory=list)

# Line 22-24: Mutates existing structure
def add_segment(self, ts: float, text: str) -> None:
    self.segments.append((ts, text))  # Mutates list
    self.total_chars += len(text)
    self.last_updated = max(self.last_updated, ts)  # Mutates field
```

**Note:** While individual tuples are immutable, the list containing them is mutated. Corrections should be represented as new events, not modifications.

---

### 3. Grounded Answering Contract Violations (CRITICAL)

**File:** `mechus/responder.py`  
**Lines:** 130-305

#### Violation 3.1: No Event IDs in Answers
- **Missing:** `OllamaResponder.respond()` returns only `str`
- **Requirement:** "Every answer includes a list of event_ids used"

**Evidence:**
```python
# Line 136: Returns only string
def respond(self, inp: ResponseInput) -> str:
    # ... generates response
    return limit_words(text, self._max_words)  # No event_ids
```

#### Violation 3.2: No Citation Mechanism
- **Missing:** No tracking of which memory segments/events were used to generate the answer
- **Requirement:** "No answer can be emitted without citations"

**Evidence:** The responder receives `context_text` and `memory_text` but does not track which specific segments were used.

#### Violation 3.3: No Explicit Refusal Path
- **Missing:** No explicit code path that refuses to answer when evidence is insufficient
- **Requirement:** "There is an explicit refusal path when evidence is insufficient"

**Evidence:** The LLM prompt instructs to use only provided context, but there's no enforcement or refusal mechanism if context is empty or insufficient.

#### Partial Pass: LLM Instruction
- ✅ **Lines 198-200:** LLM is instructed to "Use ONLY information from the meeting transcript/memory provided"
- ⚠️ **But:** No enforcement mechanism exists

---

### 4. LLM Usage Discipline Violations (ARCHITECTURAL)

**File:** `mechus/agent.py`  
**Lines:** 552-607

#### Violation 4.1: LLM Used for Routing/Activation Decisions
- **Line 479:** `_actionable_via_llm()` is called to determine if a request is actionable
- **Requirement:** LLMs must NOT be used for "routing" or "activation decisions"

**Evidence:**
```python
# Line 478-481: LLM used for routing decision
elif is_actionable and self._config.actionable_llm_enabled:
    llm_actionable = self._actionable_via_llm(request_text)
    if llm_actionable is not None:
        is_actionable = llm_actionable
```

**Impact:** This violates the architectural constraint that LLMs should only be used for Tier-2 extraction and answer synthesis.

#### Acceptable LLM Usage
- ✅ **Transcript Refinement** (`mechus/transcript.py:273`): Acceptable as Tier-2 extraction
- ✅ **Answer Synthesis** (`mechus/responder.py:130`): Acceptable

---

### 5. Deterministic Replay Readiness Violations

**File:** `mechus/memory.py`, `mechus/agent.py`  
**Multiple locations**

#### Violation 5.1: Non-Deterministic Timestamps
- **Line 139 (memory.py):** Uses `time.time()` for timestamps
- **Line 357 (memory.py):** Uses `time.time()` in NoteStore
- **Requirement:** "Event construction depends only on transcript input and deterministic rules"

**Evidence:**
```python
# Line 139: Non-deterministic timestamp
ts = time.time()

# Line 357: Non-deterministic timestamp
note = Note(timestamp_s=time.time(), ...)
```

**Impact:** Replay from saved transcript logs would produce different timestamps, breaking deterministic replay.

#### Violation 5.2: Time-Dependent Logic
- **Line 101 (memory.py):** Uses `time.time()` for recency calculations
- **Line 243 (memory.py):** Uses `time.time()` for age calculations
- **Requirement:** "No hidden global state or time-dependent logic"

**Evidence:**
```python
# Line 101: Time-dependent recency calculation
age = time.time() - topic_ctx.last_updated
recency_bonus = 1.0 / (1.0 + age / 600.0)
```

**Impact:** Replay would produce different recency scores, affecting memory retrieval.

---

## 🔧 REQUIRED FIXES

### Fix 1: Implement Deterministic Wake Word Detection
**Priority:** CRITICAL  
**File:** `mechus/wake.py`

**Changes Required:**
1. Replace Vosk/openWakeWord with pure regex/string matching on final transcript segments only
2. Add co-occurrence validation: wake word must appear in same segment as actionable pattern
3. Change return type to structured result:
   ```python
   @dataclass(frozen=True)
   class WakeActivation:
       activated: bool
       confidence: float  # Always 1.0 for deterministic
       cleaned_query: str
   ```
4. Remove partial result checking (lines 98-111)

**Minimal Change:** Create new `DeterministicWakeDetector` class that:
- Only processes `is_final == True` segments from STT
- Uses regex matching: `re.search(rf"\b{re.escape(wake_phrase)}\b", text)`
- Validates co-occurrence with actionable patterns
- Returns structured `WakeActivation` object

---

### Fix 2: Implement ContextEvent Immutability
**Priority:** CRITICAL  
**File:** `mechus/memory.py`

**Changes Required:**
1. Create `ContextEvent` dataclass:
   ```python
   @dataclass(frozen=True)
   class ContextEvent:
       event_id: str
       timestamp_s: float
       text: str
       kind: str  # "transcript", "user_request", "mechus_response", etc.
       meta: dict[str, Any]
   ```
2. Make `ConversationMemory` store immutable `ContextEvent` objects
3. Remove `add_segment()` mutation - replace with `append_event()` that creates new events
4. For corrections/reversals, create new events (do not modify existing)

**Minimal Change:** Refactor `add_segment()` to create and append new `ContextEvent` objects instead of mutating lists.

---

### Fix 3: Add Grounded Answering with Citations
**Priority:** CRITICAL  
**File:** `mechus/responder.py`

**Changes Required:**
1. Change `respond()` return type:
   ```python
   @dataclass(frozen=True)
   class GroundedResponse:
       text: str
       event_ids: list[str]
       confidence: float  # 0.0-1.0 based on evidence quality
   ```
2. Track which events were used during answer generation
3. Add explicit refusal when `event_ids` is empty or confidence < threshold
4. Modify LLM prompt to require citations in response

**Minimal Change:** 
- Modify `OllamaResponder.respond()` to track event IDs from memory retrieval
- Return `GroundedResponse` instead of `str`
- Add refusal logic when no relevant events found

---

### Fix 4: Remove LLM from Routing
**Priority:** ARCHITECTURAL  
**File:** `mechus/agent.py`

**Changes Required:**
1. Remove `_actionable_via_llm()` method (lines 552-607)
2. Remove call to `_actionable_via_llm()` (line 479)
3. Rely solely on deterministic pattern matching in `CommandValidator`

**Minimal Change:** Delete `_actionable_via_llm()` and use only `CommandValidator` for routing decisions.

---

### Fix 5: Make Replay Deterministic
**Priority:** HIGH  
**Files:** `mechus/memory.py`, `mechus/agent.py`

**Changes Required:**
1. Accept timestamp as parameter instead of using `time.time()`
2. For replay mode, use timestamps from saved transcript logs
3. Remove time-dependent recency calculations in replay mode
4. Use deterministic event ordering instead of time-based sorting

**Minimal Change:** Add optional `replay_mode: bool` parameter that disables `time.time()` usage and uses provided timestamps.

---

## 🟢 APPLY READINESS VERDICT

### **DO NOT APPLY — FIX REQUIRED**

**Reasoning:**
1. **CRITICAL:** Wake word detection uses non-deterministic neural networks instead of regex/string matching
2. **CRITICAL:** Wake word checked on partial results (violates `is_final == true` requirement)
3. **CRITICAL:** No ContextEvent immutability - events are stored in mutable structures
4. **CRITICAL:** No grounded answering contract - answers lack event IDs and citations
5. **CRITICAL:** No explicit refusal path when evidence is insufficient
6. **ARCHITECTURAL:** LLM used for routing decisions (violates usage discipline)
7. **HIGH:** Non-deterministic timestamps prevent replay readiness

**Blocking Issues Count:** 7 critical/architectural violations

**Recommendation:** Address all CRITICAL and ARCHITECTURAL violations before applying to production. The system cannot be considered safe for enterprise meetings in its current state.

---

## Additional Notes

- The codebase shows good structure and separation of concerns
- Cooldown enforcement is correctly implemented
- TTS gating is correctly implemented (only after wake word)
- The violations are architectural/safety issues, not code quality issues
- Most violations require refactoring core components, not just bug fixes

---

**End of Audit Report**
