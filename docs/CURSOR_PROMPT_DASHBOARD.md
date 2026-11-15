# Cursor Prompt for Dashboard Integration

Copy and paste this entire prompt into Cursor for your dashboard repo:

---

I have a Next.js dashboard that uses Supabase to store meetings. Users can paste meeting links (Zoom/Google Meet) into the dashboard. I need to integrate with a Mechus API server that's running on `http://127.0.0.1:5000`.

**What I need:**

1. When a user pastes a meeting link and clicks "Start" (or similar button), the dashboard should:
   - Call the Mechus API server at `http://127.0.0.1:5000/api/join-meeting`
   - Send the meeting link in the request body
   - The Mechus API will automatically open the meeting in the browser and start the Mechus voice agent

2. The API endpoint expects:
   ```json
   POST http://127.0.0.1:5000/api/join-meeting
   {
     "meeting_link": "https://zoom.us/j/123456789",
     "user_id": "user-123",  // REQUIRED - from Supabase auth
     "meeting_id": "meeting-123",  // optional, auto-generated if not provided
     "display_name": "Mechus"  // optional
   }
   ```
   
   **Important:** Each user can only have **one active meeting** at a time. If a user tries to start a second meeting, the API will return a `409 Conflict` error.

3. I need you to:
   - Find where meetings are started in my codebase (look for `triggerMechusSessionStart` function or meeting start actions)
   - Add code to call the Mechus API when a meeting is started
   - Handle errors gracefully
   - Optionally update the Supabase meeting status to reflect Mechus session state

**Current setup:**
- Dashboard is Next.js with Supabase
- Meetings are stored in Supabase `meetings` table
- There's likely a `triggerMechusSessionStart` function or similar that's currently a placeholder
- Users paste meeting links in a form/input field

**What to implement:**

1. Create a Next.js API route at `app/api/mechus/join/route.ts` that:
   - Accepts POST requests with `{ meeting_link: string, display_name?: string }`
   - Calls `http://127.0.0.1:5000/api/join-meeting`
   - Returns the response from Mechus API
   - Handles errors

2. Update the meeting start function to:
   - Call the new API route (or call Mechus API directly)
   - Update Supabase meeting status appropriately
   - Show user feedback (loading, success, error states)

3. Add environment variable:
   - `MECHUS_API_URL=http://127.0.0.1:5000` to `.env.local`

**Example code structure:**

```typescript
// app/api/mechus/join/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { meeting_link, meeting_id, display_name } = body;
    
    if (!meeting_link) {
      return NextResponse.json(
        { error: 'meeting_link is required' },
        { status: 400 }
      );
    }
    
    // Get user ID from Supabase auth
    const supabase = createClient();
    const { data: { user }, error: authError } = await supabase.auth.getUser();
    
    if (authError || !user) {
      return NextResponse.json(
        { error: 'Unauthorized' },
        { status: 401 }
      );
    }
    
    const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
    
    const response = await fetch(`${mechusApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link,
        user_id: user.id, // REQUIRED - from Supabase auth
        meeting_id: meeting_id, // Optional: from your Supabase meeting record
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

Then in your meeting start function:

```typescript
// Update your triggerMechusSessionStart or meeting start function
async function triggerMechusSessionStart(meeting: any, userId: string): Promise<void> {
  const response = await fetch('/api/mechus/join', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      meeting_link: meeting.meeting_link,
      meeting_id: meeting.id, // Use your Supabase meeting ID
      user_id: userId, // REQUIRED - from Supabase auth
      display_name: 'Mechus',
    }),
  });
  
  if (!response.ok) {
    const error = await response.json();
    
    // Handle "user already has active meeting" error
    if (error.error?.includes('already has an active meeting')) {
      throw new Error('You already have an active meeting. Please end it first.');
    }
    
    throw new Error(error.error || `HTTP ${response.status}`);
  }
  
  const data = await response.json();
  console.log('Mechus session started:', data);
}
```

**IMPORTANT: Status Polling Requirement**

After calling the Mechus API to join a meeting, the dashboard needs to:

1. **Poll the Mechus status endpoint** (`GET http://127.0.0.1:5000/api/status?meeting_id=<meeting_id>`) every 2 seconds
2. **Update the Supabase meeting status** based on Mechus's status:
   - `joining` → Keep as `joining` in Supabase
   - `active` → Update Supabase to `live` (or `active` if that's your status)
   - `stopped` → Update Supabase to `ended`
   - `error` → Update Supabase to `failed`

3. **Stop polling** when status becomes `active` or `stopped`

The Mechus API status response looks like:
```json
{
  "active": true,
  "status": "active",  // "joining", "active", "stopped", "error"
  "meeting_link": "https://...",
  "started_at": 1234567890.123,
  "uptime_seconds": 120.5,
  "transcript_chunks": 5,
  "last_activity": 1234568000.123
}
```

**Status Flow:**
- User clicks "Start" → Dashboard status: `joining`
- Mechus API called → Mechus status: `joining`
- Dashboard polls status every 2 seconds
- When Mechus status becomes `active` → Dashboard status: `live` (or `active`)
- Stop polling once status is `active` or `stopped`

**Implementation Options:**

**Option 1: Poll in server action after starting meeting**
```typescript
async function startMeeting(meetingId: string) {
  // ... call Mechus API ...
  
  // Poll for status updates
  const pollStatus = async () => {
    const response = await fetch(`http://127.0.0.1:5000/api/status?meeting_id=${meetingId}`);
    const status = await response.json();
    
    if (status.status === 'active') {
      await supabase
        .from('meetings')
        .update({ status: 'live' })
        .eq('id', meetingId);
      return true; // Stop polling
    }
    return false; // Continue polling
  };
  
  // Poll every 2 seconds for up to 60 seconds
  for (let i = 0; i < 30; i++) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    if (await pollStatus()) break;
  }
}
```

**Option 2: Use React hook to poll status (client-side)**
```typescript
// hooks/useMechusStatus.ts
export function useMechusStatus(meetingId: string) {
  const [status, setStatus] = useState(null);
  
  useEffect(() => {
    const interval = setInterval(async () => {
      // Option 1: Get status by meeting_id
      const response = await fetch(`http://127.0.0.1:5000/api/status?meeting_id=${meetingId}`);
      
      // Option 2: Get all meetings for user
      // const response = await fetch(`http://127.0.0.1:5000/api/status?user_id=${userId}`);
      
      const data = await response.json();
      setStatus(data);
      
      // Update Supabase when status changes
      if (data.status === 'active') {
        await fetch(`/api/meetings/${meetingId}/update-status`, {
          method: 'PATCH',
          body: JSON.stringify({ status: 'live' }),
        });
      }
    }, 2000);
    
    return () => clearInterval(interval);
  }, [meetingId]);
  
  return status;
}
```

**Please:**
1. Find my existing meeting start code
2. Integrate the Mechus API call
3. Add status polling to update Supabase when Mechus becomes active
4. Update the meeting status display to show "Active" when Mechus status is "active"
5. Add proper error handling
6. Show me what files were changed

---
