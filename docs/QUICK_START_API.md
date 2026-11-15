# Quick Start: Koov Meeting Automation

## What This Does

This solution allows your dashboard to automatically:
1. ✅ Open meeting links in the browser
2. ✅ Start the Koov agent automatically
3. ✅ Monitor session status

**Zero setup for the user:** The end user only uses the dashboard: paste meeting link, click Start. No .env or device setup. The server needs a one-time setup (below) by whoever deploys it; after that, users just paste links.

## Quick Setup (3 Steps)

### 1. Install Dependencies (one-time)

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

**One-time: download wake word model** (required before first join):

```bash
python3 scripts/download_vosk_model.py --out models
```

No need to set `KOOV_VOSK_MODEL_PATH` in .env; the server defaults to `models/vosk-model-small-en-us-0.15`.

### 2. Start the API Server

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

You should see: `Starting Koov API server on 127.0.0.1:5000`

### 3. Update Your Dashboard

Replace the `triggerKoovSessionStart` function in your dashboard with:

```typescript
async function triggerKoovSessionStart(meeting: any): Promise<void> {
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  
  const response = await fetch(`${koovApiUrl}/api/join-meeting`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      meeting_link: meeting.meeting_link,
      display_name: 'Koov',
      // user_id optional (default "default"); dashboard can send only meeting_link
    }),
  });
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || `HTTP ${response.status}`);
  }
}
```

## Test It

1. Make sure the API server is running
2. In your dashboard, paste a meeting link and click "Start"
3. The meeting should open in your browser
4. Koov should start listening automatically

## API Endpoints

- `POST /api/join-meeting` - Join a meeting and start Koov
- `GET /api/status` - Get current session status
- `POST /api/stop` - Stop the current session
- `GET /health` - Health check

## Full Documentation

- `MEETING_AUTOMATION.md` - Complete API documentation
- `DASHBOARD_INTEGRATION_EXAMPLE.md` - Detailed integration examples

## Troubleshooting

**"Connection refused" error:**
- Make sure the API server is running
- Check the port: `lsof -i :5000`

**"Wake word model not found" (503):**
- Run once: `python3 scripts/download_vosk_model.py --out models`, then try again.

**Meeting opens but Koov doesn't hear/speak:**
- For Koov to hear Zoom and speak into the call, do a one-time audio setup (BlackHole + Multi-Output). See `SETUP_MACOS.txt`. The user doesn't do this; the deployer or docs can describe it for the machine running Koov.

**CORS errors:**
- The API has CORS enabled by default
- If issues persist, check `koov/api_server.py` CORS settings
