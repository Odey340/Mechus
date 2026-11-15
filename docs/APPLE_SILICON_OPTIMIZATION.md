# Apple Silicon Optimization Guide

## Running Qwen-2.5-7B-Instruct on MacBook Pro M4 Max

This guide shows how to optimize Mechus for Apple Silicon (M1/M2/M3/M4) by running models locally with Metal GPU acceleration instead of using Ollama.

## Performance Comparison

| Method | Latency (200 tokens) | Memory | Setup Complexity |
|--------|---------------------|--------|------------------|
| **Ollama (current)** | ~500-1200ms | ~7-14 GB | Low (just `ollama pull`) |
| **ONNX Runtime + Metal** | ~1.5-3s | ~7 GB (8-bit) | Medium |
| **PyTorch + Metal** | ~2-4s | ~7 GB (8-bit) | Medium |
| **CPU only** | ~10-20s | ~7 GB (8-bit) | Low |

> **Recommendation**: Stick with Ollama for simplicity, but use Metal-optimized Ollama builds for best performance.

## Option 1: Optimize Ollama for Apple Silicon (Recommended)

Ollama already supports Apple Silicon with Metal acceleration. Just ensure you're using the latest version:

```bash
# Install/update Ollama
brew install ollama
# or download from https://ollama.ai

# Pull the quantized model
ollama pull qwen2.5:7b-instruct

# Verify Metal is being used
ollama run qwen2.5:7b-instruct
# Should show GPU usage in Activity Monitor
```

**Expected Performance**: ~500-800ms per response (with Metal acceleration)

## Option 2: Direct ONNX Runtime + Metal (Advanced)

For maximum control and potentially better latency, run the model directly with ONNX Runtime.

### Installation

```bash
# Install dependencies
pip install onnxruntime-gpu==1.17.1
pip install transformers==4.44.0 sentencepiece==0.2.0 accelerate==0.28.0
```

### Implementation

Create a new responder that uses ONNX Runtime:

```python
# mechus/onnx_responder.py
import onnxruntime as ort
from transformers import AutoTokenizer
import numpy as np
from mechus.responder import Responder, ResponseInput

class ONNXResponder(Responder):
    def __init__(self, model_path: str = "qwen-2.5-7b-instruct-8bit"):
        # Load tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        
        # Build ONNX session with Metal
        self.session = ort.InferenceSession(
            f"{model_path}/model.onnx",
            providers=["MetalExecutionProvider", "CPUExecutionProvider"],
        )
    
    def respond(self, inp: ResponseInput) -> str:
        # Build prompt
        prompt = self._build_prompt(inp)
        
        # Tokenize
        inputs = self.tokenizer(prompt, return_tensors="np", truncation=True, max_length=2048)
        
        # Run inference
        outputs = self.session.run(
            None,
            {
                "input_ids": inputs["input_ids"].astype(np.int64),
                "attention_mask": inputs["attention_mask"].astype(np.int64),
            },
        )
        
        # Decode
        generated_ids = outputs[0][0]
        response = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        
        # Extract response (remove prompt)
        if prompt in response:
            response = response.replace(prompt, "").strip()
        
        return response
```

**Expected Performance**: ~1.5-3s per response (with Metal)

## Option 3: PyTorch + Metal (Alternative)

Use PyTorch with Metal backend for more flexibility:

### Installation

```bash
# Install PyTorch for Apple Silicon
pip install torch==2.2.2 --index-url https://download.pytorch.org/whl/cpu

# Install transformers
pip install transformers==4.44.0 sentencepiece==0.2.0 accelerate==0.28.0 bitsandbytes
```

### Implementation

```python
# mechus/pytorch_responder.py
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from mechus.responder import Responder, ResponseInput

class PyTorchMetalResponder(Responder):
    def __init__(self, model_path: str = "qwen-2.5-7b-instruct-8bit"):
        # Load tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        
        # Load model with 8-bit quantization
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            device_map="auto",
            torch_dtype=torch.float16,  # Use FP16 for Metal
            load_in_8bit=True,  # 8-bit quantization
        )
        
        # Enable Metal backend
        if torch.backends.mps.is_available():
            self.model = self.model.to("mps")  # Metal Performance Shaders
    
    def respond(self, inp: ResponseInput) -> str:
        prompt = self._build_prompt(inp)
        inputs = self.tokenizer(prompt, return_tensors="pt")
        
        # Move to Metal device
        if torch.backends.mps.is_available():
            inputs = {k: v.to("mps") for k, v in inputs.items()}
        
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=200,
                temperature=0.2,
                top_p=0.9,
                do_sample=True,
            )
        
        response = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        if prompt in response:
            response = response.replace(prompt, "").strip()
        
        return response
```

**Expected Performance**: ~2-4s per response (with Metal)

## Memory Requirements

| Model Variant | Size | M4 Max (24 GB) | M3 Max (36 GB) | M4 Max (48 GB) |
|---------------|------|----------------|----------------|----------------|
| FP16 (7B) | ~28 GB | ❌ Too large | ✅ Fits | ✅ Fits |
| 8-bit | ~7 GB | ✅ Fits | ✅ Fits | ✅ Fits |
| 4-bit | ~3.5 GB | ✅ Fits | ✅ Fits | ✅ Fits |

**Recommendation**: Use 8-bit for best balance of speed and quality.

## Integration with Mechus

### Update Config

Add to `mechus/config.py`:

```python
llm_backend: str  # "ollama", "onnx", "pytorch"
onnx_model_path: str | None
pytorch_model_path: str | None
use_metal: bool = True
```

### Update Agent

Modify `mechus/agent.py` to support multiple backends:

```python
def _build_responder(self, config: MechusConfig):
    if config.llm_backend == "onnx" and config.onnx_model_path:
        from mechus.onnx_responder import ONNXResponder
        return ONNXResponder(model_path=config.onnx_model_path)
    elif config.llm_backend == "pytorch" and config.pytorch_model_path:
        from mechus.pytorch_responder import PyTorchMetalResponder
        return PyTorchMetalResponder(model_path=config.pytorch_model_path)
    elif config.llm_backend == "ollama" and config.ollama_model:
        return OllamaResponder(...)  # Current implementation
    else:
        return RuleResponder(...)  # Fallback
```

## Performance Optimization Tips

### 1. Use Quantized Models
- **8-bit**: Best balance (recommended)
- **4-bit**: Smallest, but slower on CPU

### 2. Enable Metal Acceleration
```python
# Check if Metal is available
import torch
if torch.backends.mps.is_available():
    print("Metal GPU available!")
```

### 3. Optimize Prompt Size
- Keep prompts under 2000 tokens
- Use context truncation (already implemented)

### 4. Batch Processing
- Process multiple requests together if possible
- Metal benefits from batch processing

### 5. Model Caching
- Pre-load model in memory
- Keep model loaded between requests

## Benchmarking

Test your setup:

```python
import time

# Test ONNX
start = time.time()
response = onnx_responder.respond(test_input)
onnx_time = time.time() - start

# Test PyTorch
start = time.time()
response = pytorch_responder.respond(test_input)
pytorch_time = time.time() - start

# Test Ollama
start = time.time()
response = ollama_responder.respond(test_input)
ollama_time = time.time() - start

print(f"ONNX: {onnx_time:.2f}s")
print(f"PyTorch: {pytorch_time:.2f}s")
print(f"Ollama: {ollama_time:.2f}s")
```

## Recommended Setup for M4 Max

```bash
# 1. Use Ollama (simplest, already optimized)
ollama pull qwen2.5:7b-instruct

# 2. Verify Metal usage
# Check Activity Monitor > GPU tab when running

# 3. Configure Mechus
MECHUS_LLM_BACKEND=ollama
MECHUS_OLLAMA_MODEL=qwen2.5:7b-instruct
MECHUS_OLLAMA_HOST=http://127.0.0.1:11434
```

**Expected Latency**: ~500-800ms per response

## Troubleshooting

### Metal Not Available
```python
import torch
print(torch.backends.mps.is_available())  # Should be True
print(torch.backends.mps.is_built())      # Should be True
```

### Out of Memory
- Use 8-bit or 4-bit quantized models
- Reduce `max_new_tokens`
- Close other applications

### Slow Performance
- Ensure Metal is being used (check Activity Monitor)
- Use quantized models
- Reduce prompt size
- Use Ollama (already optimized)

## Summary

| Approach | Latency | Setup | Recommended |
|----------|---------|-------|-------------|
| **Ollama** | ~500-800ms | Easy | ✅ Yes |
| **ONNX + Metal** | ~1.5-3s | Medium | ⚠️ Only if you need control |
| **PyTorch + Metal** | ~2-4s | Medium | ⚠️ Only for research |
| **CPU only** | ~10-20s | Easy | ❌ Too slow |

**Bottom Line**: Stick with Ollama for the best balance of performance and simplicity. Ollama already uses Metal acceleration on Apple Silicon.
