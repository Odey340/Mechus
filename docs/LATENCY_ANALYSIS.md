# Mechus Latency Analysis

## Current Latency Breakdown (After Cleanup)

### Wake Word Detection → Response Pipeline

The main latency path is: **Wake Word Detection → STT → LLM Response → TTS**

#### 1. Wake Word Detection
- **Latency**: ~10-50ms (deterministic, audio-based)
- **Method**: Vosk or openWakeWord
- **No LLM calls** ✅

#### 2. Audio Capture & STT (Speech-to-Text)
- **Latency**: ~200-800ms (depends on STT backend)
  - Faster-Whisper (local): ~200-400ms
  - OpenAI Whisper API: ~300-600ms
  - Deepgram: ~150-300ms
  - AssemblyAI: ~400-800ms
- **Bottleneck**: STT processing time

#### 3. Command Validation
- **Latency**: <1ms (rule-based regex)
- **No LLM calls** ✅ (removed actionable_llm check)

#### 4. Context Retrieval
- **Latency**: ~5-20ms (in-memory lookup)
- **No LLM calls** ✅

#### 5. LLM Response Generation
- **Latency**: ~500-2000ms (depends on model and prompt size)
  - qwen2.5:7b-instruct: ~500-1200ms
  - llama3.1:8b-instruct: ~600-1500ms
  - Larger models: ~1000-3000ms
- **Bottleneck**: LLM inference time
- **Timeout**: 30 seconds (configurable)

#### 6. TTS (Text-to-Speech)
- **Latency**: ~100-500ms (depends on backend)
  - Piper (local): ~100-200ms
  - ElevenLabs API: ~200-500ms
- **Bottleneck**: API call or local synthesis

### Total End-to-End Latency

**Typical Range**: 800ms - 3.5 seconds
- **Best Case** (local STT, fast LLM, local TTS): ~800-1500ms
- **Average Case**: ~1.5-2.5 seconds
- **Worst Case** (cloud STT, slow LLM, API TTS): ~2.5-3.5 seconds

### Latency Improvements from Cleanup

#### Before Cleanup:
1. **Background Preprocessor**: Added 0-500ms (background LLM calls)
2. **Actionable LLM Check**: Added ~300-800ms per wake word
3. **Complex Diarization Processing**: Added ~50-100ms per chunk

#### After Cleanup:
1. ✅ **Removed Background Preprocessor**: Saved 0-500ms
2. ✅ **Removed Actionable LLM Check**: Saved ~300-800ms per wake word
3. ✅ **Simplified Diarization**: Saved ~50-100ms per chunk

**Total Improvement**: ~350-1400ms faster per wake word activation

### Current Latency Bottlenecks

1. **STT Processing** (200-800ms) - Largest variable
2. **LLM Response** (500-2000ms) - Second largest
3. **TTS Synthesis** (100-500ms) - Usually acceptable
4. **Network Latency** (if using cloud services) - Variable

### Latency Targets (Not Currently Tracked)

The system doesn't currently track latency metrics in the main agent. The experimental ContextEventEngine has latency tracking, but it's not integrated.

**Recommended Targets**:
- Wake word → STT: ≤500ms p95
- STT → LLM response: ≤2000ms p95
- LLM → TTS: ≤500ms p95
- **Total end-to-end**: ≤3.0s p95

### How to Measure Latency

To add latency tracking to the main agent, you could:

1. **Add timing around key operations**:
```python
import time

# In _run_wake_cycle
start = time.time()
request_t = self._stt.transcribe_pcm16(req16, self._wake_sr).text
stt_latency = (time.time() - start) * 1000

start = time.time()
response_text = self._responder.respond(...)
llm_latency = (time.time() - start) * 1000
```

2. **Track metrics similar to ContextEventEngine**:
   - Use a rolling window of latency measurements
   - Calculate p50, p95, p99 percentiles
   - Log or expose via API

### Optimization Opportunities

1. **STT Optimization**:
   - Use faster-whisper with int8 quantization: ~200-300ms
   - Use Deepgram for cloud: ~150-300ms
   - Pre-load STT model in memory

2. **LLM Optimization**:
   - Use smaller models (qwen2.5:7b vs larger models)
   - Reduce prompt size (already optimized)
   - Use streaming responses (if supported)
   - Cache common responses

3. **TTS Optimization**:
   - Use local TTS (Piper) for lower latency
   - Pre-warm TTS engine
   - Stream audio output

4. **Parallel Processing**:
   - Start TTS while LLM is generating (if possible)
   - Pre-fetch context while STT is processing

### Current Status: Latency Tracking Enabled

**Latency is now tracked** in the main MechusAgent for each wake cycle:

- **stt_ms**: Request audio → STT (speech-to-text) for the user’s question.
- **context_ms**: Optional context-window transcription (when “summarize” / “what did” etc. are used).
- **memory_ms**: In-memory retrieval for meeting context.
- **llm_ms**: Response generation (Ollama or rule responder).
- **tts_ms**: Text-to-speech synthesis and play.
- **total_ms**: Sum of the above (set after TTS finishes).

**Where to read it**

- **API**: `GET /api/status?meeting_id=<id>` includes a `latency` object with the last wake-cycle timings (e.g. `stt_ms`, `context_ms`, `memory_ms`, `llm_ms`, `tts_ms`, `total_ms`).
- **In code**: `agent._last_latency` is updated after each successful wake cycle (and after TTS completes).

**Typical ranges (from doc above)**

- **stt_ms**: ~200–800 ms (backend-dependent).
- **context_ms**: 0 or ~200–800 ms when context window is transcribed.
- **memory_ms**: ~5–20 ms.
- **llm_ms**: ~500–2000 ms (Ollama/model-dependent).
- **tts_ms**: ~100–500 ms.
- **total_ms**: ~800 ms–3.5 s typical.

**Testing meeting Q&A and latency**

- `scripts/test_mechus_meeting_qa.py` runs a simulated meeting (pre-loaded transcript), asks a question, and reports memory + LLM latency and the final transcript. Use `--question "..."` or `--web` for a web-backed question.
