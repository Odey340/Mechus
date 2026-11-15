# Ollama Setup Guide

## Installing Ollama

### macOS
```bash
# Using Homebrew (recommended)
brew install ollama

# Or download from https://ollama.ai
```

### Linux
```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

### Windows
Download installer from https://ollama.ai

## Starting Ollama

Ollama runs as a background service. After installation, it should start automatically.

### Check if Ollama is running
```bash
# Check if Ollama service is running
ps aux | grep ollama

# Or test the API
curl http://127.0.0.1:11434/api/tags
```

### Start Ollama manually (if needed)
```bash
# macOS/Linux
ollama serve

# Or run in background
ollama serve &
```

## Pulling Models

### Qwen 2.5 7B Instruct (Recommended)
```bash
ollama pull qwen2.5:7b-instruct
```

This will download:
- Model weights (~4-7 GB depending on quantization)
- Tokenizer files
- Configuration files

**Download time**: ~5-15 minutes depending on internet speed

### Other Recommended Models

```bash
# Alternative: Llama 3.1 8B (slightly faster)
ollama pull llama3.1:8b-instruct

# For reasoning tasks
ollama pull deepseek-r1:7b
```

## Verifying Installation

### Test the model
```bash
ollama run qwen2.5:7b-instruct "Hello, how are you?"
```

### Check available models
```bash
ollama list
```

### Check model info
```bash
ollama show qwen2.5:7b-instruct
```

## Apple Silicon Optimization

On Apple Silicon (M1/M2/M3/M4), Ollama automatically uses Metal GPU acceleration:

```bash
# Verify Metal is being used
# Check Activity Monitor > GPU tab when running Ollama
ollama run qwen2.5:7b-instruct "test"
```

**Expected Performance on M4 Max**:
- **With Metal**: ~500-800ms per response
- **Without Metal (CPU)**: ~10-20s per response

## Troubleshooting

### Ollama not starting
```bash
# Check if port 11434 is in use
lsof -i :11434

# Kill existing Ollama process
pkill ollama

# Restart
ollama serve
```

### Model download fails
```bash
# Check internet connection
ping ollama.ai

# Try pulling again
ollama pull qwen2.5:7b-instruct

# Check disk space (need ~10 GB free)
df -h
```

### Out of memory errors
```bash
# Use smaller model
ollama pull qwen2.5:1.5b-instruct  # Smaller variant

# Or use quantized version
ollama pull qwen2.5:7b-instruct-q4_0  # 4-bit quantized
```

### Slow performance
1. **Check Metal is enabled** (Apple Silicon):
   - Activity Monitor > GPU tab
   - Should show GPU usage when running Ollama

2. **Use quantized models**:
   ```bash
   ollama pull qwen2.5:7b-instruct-q4_0  # 4-bit (faster, less memory)
   ollama pull qwen2.5:7b-instruct-q8_0  # 8-bit (balanced)
   ```

3. **Reduce context size** in Mechus config:
   ```bash
   MECHUS_MEMORY_MAX_CHARS=4000  # Reduced from 8000
   ```

## Configuration for Mechus

Once Ollama is running and models are pulled, configure Mechus:

```bash
# .env file
MECHUS_LLM_BACKEND=ollama
MECHUS_OLLAMA_HOST=http://127.0.0.1:11434
MECHUS_OLLAMA_MODEL=qwen2.5:7b-instruct
```

## Performance Tips

1. **Keep Ollama running**: Don't stop the service between uses
2. **Pre-warm model**: First request is slower (model loading)
3. **Use quantized models**: q4_0 or q8_0 for better performance
4. **Monitor GPU usage**: On Apple Silicon, ensure Metal is active

## Model Sizes

| Model | Size | Memory Usage | Speed (M4 Max) |
|-------|------|--------------|----------------|
| qwen2.5:7b-instruct | ~4.7 GB | ~7-10 GB | ~500-800ms |
| qwen2.5:7b-instruct-q8_0 | ~4.0 GB | ~6-8 GB | ~400-700ms |
| qwen2.5:7b-instruct-q4_0 | ~2.1 GB | ~4-6 GB | ~300-600ms |
| llama3.1:8b-instruct | ~4.7 GB | ~7-10 GB | ~600-1000ms |

**Recommendation**: Use `qwen2.5:7b-instruct` for best quality, or `qwen2.5:7b-instruct-q8_0` for better performance.
