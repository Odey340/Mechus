# Step-by-Step: Connect Dashboard to Koov

This guide walks you through connecting your dashboard (in a different repo) to the Koov API server.

## Prerequisites

- ✅ Koov repo is set up and working
- ✅ Dashboard repo is set up and working
- ✅ Both repos are on the same machine (or Koov API is accessible from dashboard)

## Step 1: Install Dependencies in Koov Repo

In your **Koov repo** (`koovdemo`):

```bash
cd /path/to/koovdemo
source .venv/bin/activate
pip install -r requirements.txt
```

This installs Flask and Flask-CORS needed for the API server.

## Step 2: Start the Koov API Server

In your **Koov repo**, start the API server:

```bash
cd /path/to/koovdemo
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

## Step 3: Test the API Server

In a new terminal, test that it's working:

```bash
curl http://127.0.0.1:5000/health
```

You should get:
```json
{"status":"ok","service":"koov-api"}
```

## Step 4: Find Your Dashboard's Meeting Start Function

In your **dashboard repo**, find where meetings are started. Look for:
- `triggerKoovSessionStart` function
- Meeting start button/action
- API route that handles meeting creation/start

Common locations:
- `lib/meetings/actions.ts`
- `app/api/meetings/[id]/start/route.ts`
- `app/api/meetings/route.ts`

## Step 5: Update Your Dashboard Code

### Option A: If you have a `triggerKoovSessionStart` function

Replace it with this:

```typescript
async function triggerKoovSessionStart(meeting: any): Promise<void> {
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  const meetingLink = meeting.meeting_link;
  
  if (!meetingLink) {
    throw new Error('Meeting link is required');
  }
  
  try {
    const response = await fetch(`${koovApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link: meetingLink,
        display_name: 'Koov',
      }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    
    const data = await response.json();
    console.log('Koov session started:', data);
  } catch (error) {
    console.error('Failed to start Koov session:', error);
    throw error;
  }
}
```

### Option B: If you're starting meetings in a server action/API route

Add this code where you handle meeting start:

```typescript
// After creating/updating the meeting in Supabase, call Koov API
const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';

const koovResponse = await fetch(`${koovApiUrl}/api/join-meeting`, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    meeting_link: meeting.meeting_link, // or wherever you store the link
    display_name: 'Koov',
  }),
});

if (!koovResponse.ok) {
  const error = await koovResponse.json();
  console.error('Koov API error:', error);
  // Handle error (maybe update meeting status to 'failed')
} else {
  const koovData = await koovResponse.json();
  console.log('Koov started successfully:', koovData);
}
```

### Option C: Create a Next.js API route (Recommended)

Create a new file in your dashboard repo:

**File:** `app/api/koov/join/route.ts`

```typescript
import { NextRequest, NextResponse } from 'next/server';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { meeting_link, display_name } = body;
    
    if (!meeting_link) {
      return NextResponse.json(
        { error: 'meeting_link is required' },
        { status: 400 }
      );
    }
    
    // Call Koov API Server
    const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
    
    const response = await fetch(`${koovApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link,
        display_name: display_name || 'Koov',
      }),
    });
    
    const data = await response.json();
    
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status });
    }
    
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error calling Koov API:', error);
    return NextResponse.json(
      { error: 'Failed to start Koov session' },
      { status: 500 }
    );
  }
}
```

Then in your meeting start function, call this route:

```typescript
// In your meeting start function
const response = await fetch('/api/koov/join', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    meeting_link: meeting.meeting_link,
    display_name: 'Koov',
  }),
});
```

## Step 6: Add Environment Variable

In your **dashboard repo**, add to `.env.local`:

```bash
KOOV_API_URL=http://127.0.0.1:5000
```

If your dashboard and Koov are on different machines, use the actual IP:
```bash
KOOV_API_URL=http://192.168.1.100:5000  # Replace with Koov machine's IP
```

## Step 7: Update Your Frontend Component

Find where users paste the meeting link and add a button/action to start Koov.

Example component:

```typescript
// components/StartKoovButton.tsx
'use client';

import { useState } from 'react';

export function StartKoovButton({ meetingLink }: { meetingLink: string }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  
  const handleStart = async () => {
    setLoading(true);
    setError(null);
    setSuccess(false);
    
    try {
      const response = await fetch('/api/koov/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          meeting_link: meetingLink,
          display_name: 'Koov',
        }),
      });
      
      const data = await response.json();
      
      if (!response.ok) {
        throw new Error(data.error || 'Failed to start Koov');
      }
      
      setSuccess(true);
      console.log('Koov started:', data);
      
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };
  
  return (
    <div>
      <button
        onClick={handleStart}
        disabled={loading || !meetingLink}
        className="px-4 py-2 bg-blue-500 text-white rounded disabled:opacity-50"
      >
        {loading ? 'Starting Koov...' : 'Start Koov Meeting'}
      </button>
      {error && (
        <p className="text-red-500 mt-2 text-sm">{error}</p>
      )}
      {success && (
        <p className="text-green-500 mt-2 text-sm">
          Koov is joining the meeting!
        </p>
      )}
    </div>
  );
}
```

## Step 8: Test the Integration

1. **Make sure Koov API server is running** (Step 2)
2. **Start your dashboard** (`npm run dev` or similar)
3. **Paste a meeting link** in your dashboard
4. **Click "Start Koov"** (or whatever button you created)
5. **Check:**
   - Meeting should open in your browser
   - Koov should start listening (check the Koov API server terminal)
   - You should see "Koov dormant. Wake phrase: computer" in the terminal

## Step 9: Verify It Works

1. **Check Koov API server terminal** - Should show:
   ```
   Opening meeting link: https://zoom.us/j/...
   Koov dormant. Wake phrase: computer
   ```

2. **Check browser** - Meeting should open automatically

3. **Test wake word** - Say "computer" in the meeting and Koov should respond

## Troubleshooting

### "Connection refused" error
- ✅ Make sure Koov API server is running (Step 2)
- ✅ Check the port: `lsof -i :5000`
- ✅ Verify `KOOV_API_URL` in dashboard `.env.local`

### Meeting opens but Koov doesn't start
- ✅ Check Koov API server terminal for errors
- ✅ Verify `.env` file in Koov repo has correct audio device settings
- ✅ Check that audio devices are configured (see `SETUP_MACOS.txt`)

### CORS errors
- ✅ Koov API has CORS enabled by default
- ✅ If issues persist, check browser console for specific error

### Different machines
- ✅ Use the actual IP address in `KOOV_API_URL`
- ✅ Make sure firewall allows port 5000
- ✅ Test connectivity: `curl http://KOOV_IP:5000/health` from dashboard machine

## Quick Checklist

- [ ] Koov API server is running
- [ ] Dashboard has `KOOV_API_URL` in `.env.local`
- [ ] Dashboard code calls `/api/koov/join` or Koov API directly
- [ ] Meeting link is passed correctly
- [ ] Test with a real meeting link

## Next Steps

Once it's working:
- Add status monitoring (see `SUPABASE_INTEGRATION.md`)
- Add error handling and user feedback
- Consider running Koov API as a service (see `MEETING_AUTOMATION.md`)
