# Meeting Automation Guide

This guide explains how to use the Mechus API server to automatically join meetings from your dashboard.

## Overview

The Mechus API server allows your dashboard to automatically:
1. Open meeting links in the browser
2. Start the Mechus agent to listen and respond in the meeting
3. Monitor the status of active sessions

## Setup

### 1. Install Dependencies

Make sure you have the latest requirements installed:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

Ensure your `.env` file is properly configured with:
- Audio device settings (`MECHUS_INPUT_DEVICE`, `MECHUS_OUTPUT_DEVICE`)
- Wake word settings
- TTS and LLM configurations

### 3. Start the API Server

Run the API server:

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

Or set environment variables:

```bash
export MECHUS_API_PORT=5000
export MECHUS_API_HOST=127.0.0.1
export MECHUS_API_DEBUG=false
python3 scripts/start_api_server.py
```

The server will start on `http://127.0.0.1:5000` by default.

## API Endpoints

### Health Check

```bash
GET /health
```

Returns server status.

### Join Meeting

```bash
POST /api/join-meeting
Content-Type: application/json

{
  "meeting_link": "https://zoom.us/j/123456789",
  "display_name": "Mechus"  # optional
}
```

**Response:**
```json
{
  "success": true,
  "message": "Mechus is joining the meeting",
  "status": "starting",
  "meeting_link": "https://zoom.us/j/123456789?uname=Mechus"
}
```

### Get Status

```bash
GET /api/status
```

**Response:**
```json
{
  "active": true,
  "status": "running",
  "meeting_link": "https://zoom.us/j/123456789",
  "started_at": 1234567890.123,
  "uptime_seconds": 120.5
}
```

### Stop Session

```bash
POST /api/stop
```

**Response:**
```json
{
  "success": true,
  "message": "Mechus session stopped",
  "meeting_link": "https://zoom.us/j/123456789"
}
```

## Dashboard Integration

### Example: Next.js API Route

In your Next.js dashboard, create an API route that calls the Mechus server:

```typescript
// app/api/mechus/join/route.ts
export async function POST(request: Request) {
  const { meeting_link, display_name } = await request.json();
  
  const response = await fetch('http://127.0.0.1:5000/api/join-meeting', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      meeting_link,
      display_name: display_name || 'Mechus',
    }),
  });
  
  const data = await response.json();
  return Response.json(data);
}
```

### Example: Frontend Component

```typescript
// components/JoinMeetingButton.tsx
'use client';

import { useState } from 'react';

export function JoinMeetingButton({ meetingLink }: { meetingLink: string }) {
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const handleJoin = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/mechus/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ meeting_link: meetingLink }),
      });
      const data = await response.json();
      if (data.success) {
        setStatus('Joining...');
      } else {
        setStatus(`Error: ${data.error}`);
      }
    } catch (error) {
      setStatus(`Error: ${error.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <button onClick={handleJoin} disabled={loading}>
      {loading ? 'Joining...' : 'Start Mechus Meeting'}
    </button>
  );
}
```

## Important Notes

### Audio Device Setup

**Before using the automation**, you must manually configure your audio devices:

1. **Install BlackHole** (if not already installed)
   - BlackHole 2ch (minimum)
   - BlackHole 16ch (recommended for better setup)

2. **Configure Zoom/Meet Audio Settings**
   - Microphone: `BlackHole 2ch`
   - Speaker: Your Multi-Output device (includes your speakers + BlackHole)

3. **Verify Mechus Configuration**
   - `MECHUS_INPUT_DEVICE=BlackHole 2ch` (or `BlackHole 16ch`)
   - `MECHUS_OUTPUT_DEVICE=BlackHole 2ch`

See `SETUP_MACOS.txt` for detailed audio routing instructions.

### Meeting Link Format

The API automatically handles:
- **Zoom links**: Adds `?uname=Mechus` parameter so Mechus appears with the correct name
- **Google Meet links**: Opens directly (no special parameters)
- **Other platforms**: Opens in default browser

### Session Management

- Only one Mechus session can be active at a time
- If you try to start a new session while one is active, you'll get a 409 Conflict error
- Use `/api/stop` to gracefully stop the current session before starting a new one

### Troubleshooting

**Server won't start:**
- Check that port 5000 is available: `lsof -i :5000`
- Try a different port: `export MECHUS_API_PORT=5001`

**Meeting doesn't open:**
- Check that the meeting link is valid
- Verify your default browser is set correctly

**Mechus doesn't hear the meeting:**
- Verify audio device configuration (see above)
- Check that Zoom/Meet microphone is set to `BlackHole 2ch`
- Check that Zoom/Meet speaker is routing to BlackHole

**CORS errors:**
- The API server has CORS enabled by default
- If you're accessing from a different origin, you may need to configure CORS settings in `mechus/api_server.py`

## Running as a Service

To run the API server as a background service on macOS, you can use `launchd`:

1. Create a plist file: `~/Library/LaunchAgents/com.mechus.api.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mechus.api</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/your/venv/bin/python3</string>
    <string>/path/to/mechusdemo/scripts/start_api_server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/mechusdemo</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>/path/to/mechusdemo</string>
    <key>MECHUS_ENV_FILE</key>
    <string>/path/to/mechusdemo/.env</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

2. Load the service:
```bash
launchctl load ~/Library/LaunchAgents/com.mechus.api.plist
```

3. Check status:
```bash
launchctl list | grep mechus
```
