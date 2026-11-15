# Troubleshooting: Nothing is Working

Let's debug step by step to find the issue.

## Step 1: Check API Server is Running

**In your terminal, run:**
```bash
curl http://127.0.0.1:5000/health
```

**Expected:** `{"status":"ok","service":"koov-api"}`

**If this fails:**
- API server is not running
- **Fix:** Start it with `./START_SERVER.sh` or `python3 scripts/start_api_server.py`

## Step 2: Check API Server Logs

**Look at the terminal where the API server is running.**

**What you should see:**
```
Starting Koov API server on 127.0.0.1:5000
 * Running on http://127.0.0.1:5000
```

**If you see errors:**
- Note the error message
- Common issues:
  - Port 5000 already in use
  - Missing dependencies (flask, flask-cors)
  - Environment file issues

## Step 3: Test Meeting Join Endpoint

**From your dashboard or terminal:**
```bash
curl -X POST http://127.0.0.1:5000/api/join-meeting \
  -H "Content-Type: application/json" \
  -d '{"meeting_link": "https://meet.google.com/test-123"}'
```

**Expected response:**
```json
{
  "success": true,
  "message": "Koov is joining the meeting",
  "status": "starting",
  "meeting_link": "https://meet.google.com/test-123"
}
```

**Check the API server terminal** - you should see:
```
Opening meeting link: https://meet.google.com/test-123
Koov dormant. Wake phrase: computer
```

## Step 4: Check for Common Issues

### Issue A: API Server Not Starting

**Symptoms:**
- `curl http://127.0.0.1:5000/health` fails
- No server logs

**Fix:**
```bash
# Make sure you're in the koovdemo directory
cd /Users/gurujaya/Documents/Coding/Visual\ Studio\ Code/koovdemo

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
pip install flask flask-cors

# Start server
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

### Issue B: Port Already in Use

**Symptoms:**
```
Address already in use
```

**Fix:**
```bash
# Find what's using port 5000
lsof -i :5000

# Kill it or use a different port
export KOOV_API_PORT=5001
python3 scripts/start_api_server.py
```

### Issue C: Dashboard Can't Connect

**Symptoms:**
- Dashboard shows "Connection refused" or network error
- CORS errors in browser console

**Fix:**
1. Make sure API server is running
2. Check `KOOV_API_URL` in dashboard `.env.local`:
   ```
   KOOV_API_URL=http://127.0.0.1:5000
   ```
3. If dashboard is on different machine, use actual IP:
   ```
   KOOV_API_URL=http://192.168.1.100:5000
   ```

### Issue D: Meeting Opens But Koov Doesn't Start

**Symptoms:**
- Meeting opens in browser
- But no "Koov dormant" message in logs
- Status shows "stopped" or "error"

**Check:**
1. Look for errors in API server terminal
2. Check `.env` file exists and has correct settings
3. Verify audio devices are configured:
   ```
   KOOV_INPUT_DEVICE=BlackHole 2ch
   KOOV_OUTPUT_DEVICE=BlackHole 2ch
   ```

### Issue E: Signal Handler Error (Already Fixed)

**Symptoms:**
```
ValueError: signal only works in main thread
```

**Fix:** This should already be fixed. If you still see it:
1. Stop the server (Ctrl+C)
2. Pull latest code or verify the fix is in `koov/api_server.py`
3. Restart server

## Step 5: Check Dashboard Integration

**If dashboard is calling the API:**

1. **Check browser console** (F12 → Console tab)
   - Look for errors
   - Check network requests

2. **Check dashboard terminal/logs**
   - Look for API call errors
   - Verify the meeting link is being sent

3. **Test API directly:**
   ```bash
   curl -X POST http://127.0.0.1:5000/api/join-meeting \
     -H "Content-Type: application/json" \
     -d '{"meeting_link": "YOUR_MEETING_LINK_HERE"}'
   ```

## Step 6: Verify Environment Setup

**Check `.env` file exists and has:**
```bash
# Audio devices
KOOV_INPUT_DEVICE=BlackHole 2ch
KOOV_OUTPUT_DEVICE=BlackHole 2ch

# Wake word
KOOV_WAKE_PHRASE=computer
KOOV_WAKE_BACKEND=vosk
KOOV_VOSK_MODEL_PATH=./models/vosk-model-small-en-us-0.15

# STT
KOOV_STT_BACKEND=faster-whisper
KOOV_WHISPER_MODEL=base.en

# TTS (if using ElevenLabs)
KOOV_TTS_BACKEND=elevenlabs
KOOV_ELEVEN_API_KEY=your_key
KOOV_ELEVEN_VOICE_ID=your_voice_id

# LLM (if using Ollama)
KOOV_LLM_BACKEND=ollama
KOOV_OLLAMA_MODEL=your_model
```

## Quick Diagnostic Script

Run this to check everything:

```bash
#!/bin/bash
echo "=== Koov Diagnostic ==="
echo ""
echo "1. Checking API server..."
if curl -s http://127.0.0.1:5000/health > /dev/null; then
  echo "✅ API server is running"
else
  echo "❌ API server is NOT running"
  echo "   Start it with: ./START_SERVER.sh"
  exit 1
fi

echo ""
echo "2. Checking status..."
STATUS=$(curl -s http://127.0.0.1:5000/api/status)
echo "$STATUS" | jq .

echo ""
echo "3. Checking .env file..."
if [ -f .env ]; then
  echo "✅ .env file exists"
  if grep -q "KOOV_INPUT_DEVICE" .env; then
    echo "✅ Audio devices configured"
  else
    echo "⚠️  Audio devices not configured in .env"
  fi
else
  echo "❌ .env file not found"
fi

echo ""
echo "4. Checking models..."
if [ -d "models/vosk-model-small-en-us-0.15" ]; then
  echo "✅ Vosk model found"
else
  echo "❌ Vosk model not found"
  echo "   Run: python3 scripts/download_vosk_model.py --out models"
fi

echo ""
echo "=== Diagnostic Complete ==="
```

## Still Not Working?

**Please provide:**
1. What happens when you try to join a meeting?
2. What do you see in the API server terminal?
3. What errors appear (if any)?
4. Can you access `http://127.0.0.1:5000/health`?

**Common issues checklist:**
- [ ] API server is running
- [ ] Health endpoint responds
- [ ] `.env` file exists and is configured
- [ ] Audio devices are set up (BlackHole installed)
- [ ] Dashboard can reach API server
- [ ] No errors in API server terminal
- [ ] Meeting link is valid

---

## Docker: Container exited with nanobind / SIGSEGV

If you see logs like:

```
nanobind: leaked 15 instances!
Worker (pid:19) was sent SIGSEGV!
koovdemo_koov_1 exited with code 0
```

**What happened:** The container received SIGTERM (e.g. `docker stop`, closing the terminal, or VM reboot). The Zoom SDK’s Python bindings don’t tear down cleanly when the process is killed, so you get “nanobind leaked” and sometimes SIGSEGV during exit. The container has stopped.

**What to do:** Restart the stack. From your Mac: `./run.sh deploy`. Or on the VM: `cd ~/koovdemo && docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d`. The API will be back; the leak/SIGSEGV only occurs on shutdown, not during normal runs.
