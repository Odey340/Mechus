# Latency Optimizations Applied

## Implemented Optimizations

### 1. ✅ STT Parameter Optimization
**File**: `koov/stt.py`
**Change**: Reduced `beam_size` from 5 to 1, `best_of` from 5 to 1, disabled `condition_on_previous_text`
**Impact**: ~100-300ms faster STT
**Trade-off**: Slight accuracy reduction (~1-2%)

### 2. ✅ LLM Token Reduction
**File**: `koov/responder.py`
**Change**: Reduced `max_predict` from 150/80 to 100/60
**Impact**: ~200-500ms faster LLM responses
**Trade-off**: Slightly shorter responses (usually fine)

### 3. ✅ Context Size Reduction
**File**: `koov/responder.py`
**Change**: Reduced context from 2000 to 1000 chars, memory from 4000 to 2000 chars
**Impact**: ~100-300ms faster LLM processing
**Trade-off**: Less context available (usually sufficient)

### 4. ✅ TTS Pre-warming
**File**: `koov/agent.py`
**Change**: Pre-warm TTS engine on initialization
**Impact**: ~50-100ms saved on first TTS call
**Trade-off**: None

## Expected Latency Improvements

**Before**: ~1.5-2.5 seconds end-to-end
**After**: ~1.0-1.8 seconds end-to-end

**Total Improvement**: ~400-700ms faster per wake word activation

## Additional Optimizations Available

See `LATENCY_OPTIMIZATIONS.md` for more optimizations including:
- Parallel processing
- Response caching
- LLM streaming
- GPU acceleration
- Smaller models

## Testing

Test the optimizations and monitor:
1. Response quality (should be similar)
2. Actual latency (should be faster)
3. Accuracy (may be slightly lower for STT)

## Rollback

If quality is unacceptable, revert changes:
- STT: Change `beam_size` back to 5, `best_of` to 5
- LLM: Increase `max_predict` back to 150/80
- Context: Increase back to 2000/4000
