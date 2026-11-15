# Quick Start Guide

## Prerequisites Check

✅ **Ollama installed**: `/usr/local/bin/ollama`  
✅ **Model downloaded**: `qwen2.5:7b-instruct` (4.7 GB)  
✅ **Other models available**: `Eomer/gpt-3.5-turbo:latest`, `gpt-oss:20b`

## Configuration

Create or update your `.env` file:

```bash
# LLM Configuration
KOOV_LLM_BACKEND=ollama
KOOV_OLLAMA_HOST=http://127.0.0.1:11434
KOOV_OLLAMA_MODEL=qwen2.5:7b-instruct

# Wake Word
KOOV_WAKE_PHRASE=koov

# STT (Speech-to-Text)
KOOV_STT_BACKEND=faster-whisper
KOOV_WHISPER_MODEL=base.en

# TTS (Text-to-Speech)
KOOV_TTS_BACKEND=piper
# Or use ElevenLabs for better quality:
# KOOV_TTS_BACKEND=elevenlabs
# KOOV_ELEVEN_API_KEY=your_key
# KOOV_ELEVEN_VOICE_ID=your_voice_id
```

## Start Ollama (if not running)

```bash
# Check if running
curl http://127.0.0.1:11434/api/tags

# If not running, start it
ollama serve
```

## Run Koov

```bash
# Activate virtual environment (if using one)
source .venv/bin/activate

# Run Koov
python main.py
```

## Expected Performance

With `qwen2.5:7b-instruct` on M4 Max:
- **LLM Response**: ~500-800ms (with Metal GPU acceleration)
- **Total End-to-End**: ~1.0-1.8 seconds (wake word → spoken response)
- **STT**: ~200-400ms (faster-whisper)
- **TTS**: ~100-200ms (Piper) or ~200-400ms (ElevenLabs)

## Testing

1. **Test Ollama directly**:
   ```bash
   ollama run qwen2.5:7b-instruct "Hello, test"
   ```

2. **Test Koov wake word**:
   - Say "Koov, what can you do?"
   - Should respond in ~1-2 seconds

3. **Check logs**:
   - Look for "Wake detected" messages
   - Check for any error messages

## Troubleshooting

### Ollama not responding
```bash
# Restart Ollama
pkill ollama
ollama serve
```

### Model not found
```bash
# Verify model is available
ollama list

# Re-pull if needed
ollama pull qwen2.5:7b-instruct
```

### Slow responses
- Check if Metal GPU is being used (Activity Monitor > GPU)
- Ensure Ollama is using the correct model
- Check network latency if using cloud STT/TTS

## Next Steps

- See [HOW_TO_RUN_KOOV.md](HOW_TO_RUN_KOOV.md) for detailed setup
- See [HIGH_ACCURACY_LOW_LATENCY.md](HIGH_ACCURACY_LOW_LATENCY.md) for optimization
- See [APPLE_SILICON_OPTIMIZATION.md](APPLE_SILICON_OPTIMIZATION.md) for M4 Max tips
