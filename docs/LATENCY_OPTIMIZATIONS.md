# Latency Optimization Guide

## Quick Wins (Easy to Implement)

### 1. Optimize STT Parameters ⚡ **Save 100-300ms**

**Current**: `beam_size=5, best_of=5` (slow but accurate)
**Optimized**: `beam_size=1, best_of=1` (fast, slightly less accurate)

```python
# In mechus/stt.py, FasterWhisperTranscriber.transcribe_pcm16()
segments, info = self._model.transcribe(
    audio,
    language=self._language,
    vad_filter=True,
    beam_size=1,        # Changed from 5
    best_of=1,          # Changed from 5
    temperature=0.0,
    condition_on_previous_text=False,  # Disable for speed
    word_timestamps=False,
    vad_parameters=dict(min_silence_duration_ms=500),
)
```

**Impact**: Reduces STT latency from 200-400ms to 100-200ms
**Trade-off**: Slight accuracy reduction (~1-2%)

### 2. Reduce LLM Max Tokens ⚡ **Save 200-500ms**

**Current**: `max_predict=150` for decisions, `80` for others
**Optimized**: Reduce to `max_predict=60` for most, `100` for decisions

```python
# In mechus/responder.py, OllamaResponder.respond()
max_predict = 100 if is_decision else 60  # Reduced from 150/80
```

**Impact**: Faster LLM responses, especially for short answers
**Trade-off**: Slightly shorter responses (usually fine for meeting context)

### 3. Use Smaller STT Model ⚡ **Save 100-200ms**

**Current**: Using `base.en` or larger
**Optimized**: Use `tiny.en` or `small.en` for faster transcription

```bash
# Set in .env
MECHUS_WHISPER_MODEL=tiny.en  # or small.en
```

**Impact**: 2-3x faster transcription
**Trade-off**: Slightly lower accuracy (~2-3% WER increase)

### 4. Disable Transcript Refiner (if enabled) ⚡ **Save 200-500ms**

**Current**: May be refining transcripts with LLM
**Optimized**: Disable if not critical

```bash
# In .env
MECHUS_TRANSCRIPT_MODEL=  # Leave empty to disable
```

**Impact**: Eliminates extra LLM call per transcript chunk
**Trade-off**: Slightly less clean transcripts

## Medium Effort Optimizations

### 5. Parallel Processing ⚡ **Save 200-400ms**

**Current**: Sequential: STT → Context → LLM → TTS
**Optimized**: Parallel: STT + Context retrieval simultaneously

```python
# In mechus/agent.py, _run_wake_cycle()
import concurrent.futures

# Start STT and context retrieval in parallel
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    stt_future = executor.submit(
        self._stt.transcribe_pcm16, req16, self._wake_sr
    )
    context_future = executor.submit(
        self._get_context_parallel, context_pcm16
    )
    
    request_t = stt_future.result().text
    context_text = context_future.result()
```

**Impact**: Overlaps STT and context processing
**Complexity**: Medium (requires thread safety)

### 6. LLM Response Streaming ⚡ **Save 300-800ms perceived latency**

**Current**: Wait for full response before starting TTS
**Optimized**: Stream LLM response, start TTS as soon as first words arrive

```python
# In mechus/responder.py
body = {
    "model": self._model,
    "prompt": prompt,
    "stream": True,  # Enable streaming
    "options": {"temperature": temperature, "num_predict": max_predict},
}

# Stream response and start TTS early
response_parts = []
for chunk in stream_response(req):
    response_parts.append(chunk)
    if len(response_parts) == 1:  # Start TTS after first chunk
        # Start TTS in background
        pass
```

**Impact**: Perceived latency reduced by 300-800ms (user hears response sooner)
**Complexity**: Medium (requires streaming implementation)

### 7. Response Caching ⚡ **Save 500-2000ms for repeated questions**

**Current**: No caching
**Optimized**: Cache common responses

```python
# Simple cache for common questions
_response_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 60.0  # 1 minute

def get_cached_response(self, question: str) -> str | None:
    question_l = question.lower().strip()
    if question_l in _response_cache:
        response, timestamp = _response_cache[question_l]
        if time.time() - timestamp < CACHE_TTL:
            return response
    return None
```

**Impact**: Instant responses for repeated questions
**Complexity**: Low

### 8. Pre-warm TTS Engine ⚡ **Save 50-100ms**

**Current**: TTS engine initialized on first use
**Optimized**: Pre-warm with dummy synthesis

```python
# In MechusAgent.__init__()
# Pre-warm TTS
try:
    self._tts.synthesize("test")  # Warm up
except Exception:
    pass
```

**Impact**: Eliminates first-use TTS latency
**Complexity**: Low

## Advanced Optimizations

### 9. Use GPU for STT (if available) ⚡ **Save 100-300ms**

**Current**: CPU-only faster-whisper
**Optimized**: Use GPU with CUDA

```python
# In mechus/stt.py
self._model = WhisperModel(
    model, 
    device="cuda",  # or "cuda:0"
    compute_type="float16"  # Faster than int8 on GPU
)
```

**Impact**: 2-3x faster STT on GPU
**Requirements**: CUDA-capable GPU, CUDA toolkit

### 10. Use Faster LLM Models ⚡ **Save 300-800ms**

**Current**: qwen2.5:7b-instruct (~500-1200ms)
**Optimized**: Use smaller/faster models

```bash
# Faster options:
ollama pull qwen2.5:1.5b-instruct  # ~200-500ms
ollama pull phi3:mini              # ~300-600ms
ollama pull tinyllama              # ~150-400ms (lower quality)
```

**Impact**: 2-3x faster LLM responses
**Trade-off**: Lower quality responses

### 11. Optimize Context Size ⚡ **Save 100-300ms**

**Current**: Sending up to 4000 chars of memory
**Optimized**: Reduce context window

```python
# In mechus/responder.py
memory = memory[-2000:]  # Reduced from 4000
context = context[-1000:]  # Reduced from 2000
```

**Impact**: Faster LLM processing (smaller prompts)
**Trade-off**: Less context available

### 12. Use Deepgram for STT (Cloud) ⚡ **Save 100-200ms**

**Current**: Local faster-whisper
**Optimized**: Use Deepgram Nova-2 (fastest cloud option)

```bash
# In .env
MECHUS_STT_BACKEND=deepgram
MECHUS_DEEPGRAM_API_KEY=your_key
MECHUS_DEEPGRAM_MODEL=nova-2
```

**Impact**: ~150-300ms STT latency (faster than local)
**Trade-off**: Requires API key, costs money

## Aggressive Optimizations (May Impact Quality)

### 13. Skip Context Transcription ⚡ **Save 200-800ms**

**Current**: Transcribing context audio for better responses
**Optimized**: Skip context transcription, use only memory

```python
# In mechus/agent.py, _run_wake_cycle()
# Skip this block:
# if needs_context and self._config.context_seconds > 0 and context_pcm16:
#     context_text = self._stt.transcribe_pcm16(...)
context_text = ""  # Use only memory
```

**Impact**: Eliminates context STT call
**Trade-off**: Less accurate responses for recent context

### 14. Use Rule-Based Responses More ⚡ **Save 500-2000ms**

**Current**: LLM for most responses
**Optimized**: Expand rule-based responder

```python
# In mechus/responder.py, RuleResponder.respond()
# Add more patterns for common questions
if "action items" in request_l:
    return _find_action_items(combined_context)
if "decisions" in request_l:
    return _find_decisions(combined_context)
# ... more patterns
```

**Impact**: Instant responses for common questions
**Trade-off**: Less natural responses

### 15. Reduce Audio Buffer Sizes ⚡ **Save 50-100ms**

**Current**: May be buffering too much audio
**Optimized**: Reduce buffer sizes

```python
# In config
MECHUS_MIN_REQUEST_SECONDS=0.4  # Reduced from 0.6
MECHUS_END_SILENCE_SECONDS=0.6   # Reduced from 0.8
```

**Impact**: Faster wake word → response cycle
**Trade-off**: May miss end of speech

## Implementation Priority

### Phase 1: Quick Wins (Implement First)
1. ✅ Optimize STT parameters (beam_size=1)
2. ✅ Reduce LLM max tokens
3. ✅ Use smaller STT model (tiny.en)
4. ✅ Disable transcript refiner
5. ✅ Pre-warm TTS

**Expected Improvement**: 400-800ms faster

### Phase 2: Medium Effort
6. ✅ Response caching
7. ✅ Parallel processing (STT + context)
8. ✅ Optimize context size

**Expected Improvement**: Additional 300-600ms faster

### Phase 3: Advanced (If Needed)
9. ✅ GPU acceleration
10. ✅ LLM streaming
11. ✅ Faster LLM models

**Expected Improvement**: Additional 500-1000ms faster

## Total Potential Improvement

**Current**: ~1.5-2.5 seconds
**After Phase 1**: ~0.8-1.5 seconds
**After Phase 2**: ~0.5-1.0 seconds
**After Phase 3**: ~0.3-0.7 seconds

## Recommended Configuration for Low Latency

```bash
# .env file
MECHUS_WHISPER_MODEL=tiny.en
MECHUS_WHISPER_COMPUTE_TYPE=int8
MECHUS_STT_BACKEND=faster-whisper
MECHUS_OLLAMA_MODEL=qwen2.5:1.5b-instruct
MECHUS_MAX_RESPONSE_WORDS=30
MECHUS_TRANSCRIPT_MODEL=  # Disabled
MECHUS_MEMORY_RECENT_CHARS=1000  # Reduced
```

## Monitoring

Add latency tracking to measure improvements:

```python
import time

start = time.time()
response = self._responder.respond(...)
latency = (time.time() - start) * 1000
log.info(f"LLM latency: {latency:.1f}ms")
```

## Notes

- **Quality vs Speed**: Most optimizations trade some quality for speed
- **Hardware Dependent**: GPU optimizations require specific hardware
- **Use Case Dependent**: Some optimizations may not fit your use case
- **Test Thoroughly**: Always test after optimizations to ensure quality is acceptable
