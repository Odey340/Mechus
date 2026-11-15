# How to Run Koov

## Quick Start (3 Steps)

### 1. Start the API Server

In your terminal, navigate to the koovdemo directory:

```bash
cd /Users/gurujaya/Documents/Coding/Visual\ Studio\ Code/koovdemo
```

Then run:

```bash
./START_SERVER.sh
```

**OR manually:**

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

You should see:
```
Starting Koov API server on 127.0.0.1:5000
 * Running on http://127.0.0.1:5000
```

**Keep this terminal open** - the server needs to keep running.

### 2. Start a Meeting from Your Dashboard

1. Open your dashboard in the browser
2. Paste a meeting link (Zoom/Google Meet)
3. Click "Start" or "Join Meeting"
4. The meeting will open automatically in your browser
5. Koov will start listening

### 3. Verify Koov is Working

**Check the API server terminal** - you should see:
```
Opening meeting link: https://...
Koov dormant. Wake phrase: computer
```

**In the meeting:**
- Say **"computer"** to wake Koov
- Ask a question like: **"computer, summarize the last 5 minutes"**
- Koov should respond

## Alternative: Run Koov Directly (Without API Server)

If you want to run Koov the old way (without the dashboard integration):

```bash
cd /Users/gurujaya/Documents/Coding/Visual\ Studio\ Code/koovdemo
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/zoom_demo.py
```

When prompted, paste your meeting link or press Enter if you've already joined manually.

## Troubleshooting

### API Server Won't Start

**Check if port 5000 is in use:**
```bash
lsof -i :5000
```

**If port is busy, use a different port:**
```bash
export KOOV_API_PORT=5001
python3 scripts/start_api_server.py
```

### Flask Not Installed

```bash
source .venv/bin/activate
pip install flask flask-cors
```

### Koov Doesn't Respond

1. **Check audio devices** - Make sure BlackHole is configured (see `SETUP_MACOS.txt`)
2. **Check `.env` file** - Verify audio device settings:
   ```
   KOOV_INPUT_DEVICE=BlackHole 2ch
   KOOV_OUTPUT_DEVICE=BlackHole 2ch
   ```
3. **Check meeting audio settings** - Microphone should be set to BlackHole in Zoom/Meet

### Test API Server

```bash
curl http://127.0.0.1:5000/health
```

Should return: `{"status":"ok","service":"koov-api"}`

## What Happens When You Run It

1. **API Server starts** → Listens on port 5000
2. **Dashboard calls API** → Sends meeting link
3. **Koov opens meeting** → Browser opens automatically
4. **Koov starts listening** → "Koov dormant. Wake phrase: computer"
5. **You say "computer"** → Koov wakes up
6. **You ask a question** → Koov responds
7. **Transcripts are saved** → Available via `/api/transcript`

## Quick Commands Reference

```bash
# Start API server
./START_SERVER.sh

# Or manually
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py

# Test API
curl http://127.0.0.1:5000/health

# Check status
curl http://127.0.0.1:5000/api/status

# Get transcript
curl http://127.0.0.1:5000/api/transcript
```

## Next Steps

Once Koov is running:
- ✅ Join meetings from your dashboard
- ✅ Status will update from "Joining" to "Active"
- ✅ Transcripts will be available
- ✅ Action items will be extracted
- ✅ Meeting summaries will be generated
