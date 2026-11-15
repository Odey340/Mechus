# Quick Start Guide

## Prerequisites Check

✅ **Ollama installed**: `/usr/local/bin/ollama`  
✅ **Model downloaded**: `qwen2.5:7b-instruct` (4.7 GB)  
✅ **Other models available**: `Eomer/gpt-3.5-turbo:latest`, `gpt-oss:20b`

## Configuration

Create or update your `.env` file:

```bash
# LLM Configuration
MECHUS_LLM_BACKEND=ollama
MECHUS_OLLAMA_HOST=http://127.0.0.1:11434
MECHUS_OLLAMA_MODEL=qwen2.5:7b-instruct

# Wake Word
MECHUS_WAKE_PHRASE=mechus

# STT (Speech-to-Text)
MECHUS_STT_BACKEND=faster-whisper
MECHUS_WHISPER_MODEL=base.en

# TTS (Text-to-Speech)
MECHUS_TTS_BACKEND=piper
# Or use ElevenLabs for better quality:
# MECHUS_TTS_BACKEND=elevenlabs
# MECHUS_ELEVEN_API_KEY=your_key
# MECHUS_ELEVEN_VOICE_ID=your_voice_id
```

## Start Ollama (if not running)

```bash
# Check if running
curl http://127.0.0.1:11434/api/tags

# If not running, start it
ollama serve
```

## Run Mechus

```bash
# Activate virtual environment (if using one)
source .venv/bin/activate

# Run Mechus
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

2. **Test Mechus wake word**:
   - Say "Mechus, what can you do?"
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

- See [HOW_TO_RUN_MECHUS.md](HOW_TO_RUN_MECHUS.md) for detailed setup
- See [HIGH_ACCURACY_LOW_LATENCY.md](HIGH_ACCURACY_LOW_LATENCY.md) for optimization
- See [APPLE_SILICON_OPTIMIZATION.md](APPLE_SILICON_OPTIMIZATION.md) for M4 Max tips
