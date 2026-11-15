# Step-by-Step: Connect Dashboard to Mechus

This guide walks you through connecting your dashboard (in a different repo) to the Mechus API server.

## Prerequisites

- ✅ Mechus repo is set up and working
- ✅ Dashboard repo is set up and working
- ✅ Both repos are on the same machine (or Mechus API is accessible from dashboard)

## Step 1: Install Dependencies in Mechus Repo

In your **Mechus repo** (`mechusdemo`):

```bash
cd /path/to/mechusdemo
source .venv/bin/activate
pip install -r requirements.txt
```

This installs Flask and Flask-CORS needed for the API server.

## Step 2: Start the Mechus API Server

In your **Mechus repo**, start the API server:

```bash
cd /path/to/mechusdemo
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

You should see:
```
Starting Mechus API server on 127.0.0.1:5000
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
{"status":"ok","service":"mechus-api"}
```

## Step 4: Find Your Dashboard's Meeting Start Function

In your **dashboard repo**, find where meetings are started. Look for:
- `triggerMechusSessionStart` function
- Meeting start button/action
- API route that handles meeting creation/start

Common locations:
- `lib/meetings/actions.ts`
- `app/api/meetings/[id]/start/route.ts`
- `app/api/meetings/route.ts`

## Step 5: Update Your Dashboard Code

### Option A: If you have a `triggerMechusSessionStart` function

Replace it with this:

```typescript
async function triggerMechusSessionStart(meeting: any): Promise<void> {
  const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
  const meetingLink = meeting.meeting_link;
  
  if (!meetingLink) {
    throw new Error('Meeting link is required');
  }
  
  try {
    const response = await fetch(`${mechusApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link: meetingLink,
        display_name: 'Mechus',
      }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    
    const data = await response.json();
    console.log('Mechus session started:', data);
  } catch (error) {
    console.error('Failed to start Mechus session:', error);
    throw error;
  }
}
```

### Option B: If you're starting meetings in a server action/API route

Add this code where you handle meeting start:

```typescript
// After creating/updating the meeting in Supabase, call Mechus API
const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';

const mechusResponse = await fetch(`${mechusApiUrl}/api/join-meeting`, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    meeting_link: meeting.meeting_link, // or wherever you store the link
    display_name: 'Mechus',
  }),
});

if (!mechusResponse.ok) {
  const error = await mechusResponse.json();
  console.error('Mechus API error:', error);
  // Handle error (maybe update meeting status to 'failed')
} else {
  const mechusData = await mechusResponse.json();
  console.log('Mechus started successfully:', mechusData);
}
```

### Option C: Create a Next.js API route (Recommended)

Create a new file in your dashboard repo:

**File:** `app/api/mechus/join/route.ts`

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
    
    // Call Mechus API Server
    const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
    
    const response = await fetch(`${mechusApiUrl}/api/join-meeting`, {
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
    
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status });
    }
    
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error calling Mechus API:', error);
    return NextResponse.json(
      { error: 'Failed to start Mechus session' },
      { status: 500 }
    );
  }
}
```

Then in your meeting start function, call this route:

```typescript
// In your meeting start function
const response = await fetch('/api/mechus/join', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    meeting_link: meeting.meeting_link,
    display_name: 'Mechus',
  }),
});
```

## Step 6: Add Environment Variable

In your **dashboard repo**, add to `.env.local`:

```bash
MECHUS_API_URL=http://127.0.0.1:5000
```

If your dashboard and Mechus are on different machines, use the actual IP:
```bash
MECHUS_API_URL=http://192.168.1.100:5000  # Replace with Mechus machine's IP
```

## Step 7: Update Your Frontend Component

Find where users paste the meeting link and add a button/action to start Mechus.

Example component:

```typescript
// components/StartMechusButton.tsx
'use client';

import { useState } from 'react';

export function StartMechusButton({ meetingLink }: { meetingLink: string }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  
  const handleStart = async () => {
    setLoading(true);
    setError(null);
    setSuccess(false);
    
    try {
      const response = await fetch('/api/mechus/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          meeting_link: meetingLink,
          display_name: 'Mechus',
        }),
      });
      
      const data = await response.json();
      
      if (!response.ok) {
        throw new Error(data.error || 'Failed to start Mechus');
      }
      
      setSuccess(true);
      console.log('Mechus started:', data);
      
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
        {loading ? 'Starting Mechus...' : 'Start Mechus Meeting'}
      </button>
      {error && (
        <p className="text-red-500 mt-2 text-sm">{error}</p>
      )}
      {success && (
        <p className="text-green-500 mt-2 text-sm">
          Mechus is joining the meeting!
        </p>
      )}
    </div>
  );
}
```

## Step 8: Test the Integration

1. **Make sure Mechus API server is running** (Step 2)
2. **Start your dashboard** (`npm run dev` or similar)
3. **Paste a meeting link** in your dashboard
4. **Click "Start Mechus"** (or whatever button you created)
5. **Check:**
   - Meeting should open in your browser
   - Mechus should start listening (check the Mechus API server terminal)
   - You should see "Mechus dormant. Wake phrase: computer" in the terminal

## Step 9: Verify It Works

1. **Check Mechus API server terminal** - Should show:
   ```
   Opening meeting link: https://zoom.us/j/...
   Mechus dormant. Wake phrase: computer
   ```

2. **Check browser** - Meeting should open automatically

3. **Test wake word** - Say "computer" in the meeting and Mechus should respond

## Troubleshooting

### "Connection refused" error
- ✅ Make sure Mechus API server is running (Step 2)
- ✅ Check the port: `lsof -i :5000`
- ✅ Verify `MECHUS_API_URL` in dashboard `.env.local`

### Meeting opens but Mechus doesn't start
- ✅ Check Mechus API server terminal for errors
- ✅ Verify `.env` file in Mechus repo has correct audio device settings
- ✅ Check that audio devices are configured (see `SETUP_MACOS.txt`)

### CORS errors
- ✅ Mechus API has CORS enabled by default
- ✅ If issues persist, check browser console for specific error

### Different machines
- ✅ Use the actual IP address in `MECHUS_API_URL`
- ✅ Make sure firewall allows port 5000
- ✅ Test connectivity: `curl http://MECHUS_IP:5000/health` from dashboard machine

## Quick Checklist

- [ ] Mechus API server is running
- [ ] Dashboard has `MECHUS_API_URL` in `.env.local`
- [ ] Dashboard code calls `/api/mechus/join` or Mechus API directly
- [ ] Meeting link is passed correctly
- [ ] Test with a real meeting link

## Next Steps

Once it's working:
- Add status monitoring (see `SUPABASE_INTEGRATION.md`)
- Add error handling and user feedback
- Consider running Mechus API as a service (see `MEETING_AUTOMATION.md`)
