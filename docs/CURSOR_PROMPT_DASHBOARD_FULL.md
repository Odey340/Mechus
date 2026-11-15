# Complete Cursor Prompt for Dashboard Integration

Copy and paste this ENTIRE prompt into Cursor for your dashboard repo:

---

I have a Next.js dashboard that uses Supabase to store meetings. Users can paste meeting links (Zoom/Google Meet) into the dashboard. I need to integrate with a Koov API server that's running on `http://127.0.0.1:5000`.

**What I need:**

1. When a user pastes a meeting link and clicks "Start" (or similar button), the dashboard should:
   - Call the Koov API server at `http://127.0.0.1:5000/api/join-meeting`
   - Send the meeting link in the request body
   - The Koov API will automatically open the meeting in the browser and start the Koov voice agent

2. The API endpoint expects:
   ```json
   POST http://127.0.0.1:5000/api/join-meeting
   {
     "meeting_link": "https://zoom.us/j/123456789",
     "display_name": "Koov"  // optional
   }
   ```

3. **CRITICAL: Status Polling**
   After calling the Koov API, the dashboard MUST:
   - Poll `GET http://127.0.0.1:5000/api/status` every 2 seconds
   - Update the Supabase meeting status based on Koov's status:
     - `joining` → Keep as `joining` in Supabase
     - `active` → Update Supabase to `live` (or `active` if that's your status)
     - `stopped` → Update Supabase to `ended`
     - `error` → Update Supabase to `failed`
   - Stop polling when status becomes `active` or `stopped`

4. The status response from Koov API looks like:
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

**Current setup:**
- Dashboard is Next.js with Supabase
- Meetings are stored in Supabase `meetings` table
- There's likely a `triggerKoovSessionStart` function or similar that's currently a placeholder
- Users paste meeting links in a form/input field
- Meeting status is displayed in the UI (currently shows "Joining" when meeting starts)

**What to implement:**

1. Create a Next.js API route at `app/api/koov/join/route.ts` that:
   - Accepts POST requests with `{ meeting_link: string, display_name?: string }`
   - Calls `http://127.0.0.1:5000/api/join-meeting`
   - Returns the response from Koov API
   - Handles errors

2. Update the meeting start function to:
   - Call the new API route (or call Koov API directly)
   - Update Supabase meeting status to `joining` initially
   - Start polling Koov status every 2 seconds
   - Update Supabase status to `live` when Koov status becomes `active`
   - Handle errors and update status to `failed` if needed

3. Add status polling (choose one approach):

   **Option A: Server-side polling in the start function**
   ```typescript
   async function startMeeting(meetingId: string) {
     const supabase = createClient();
     
     // Update to joining
     await supabase
       .from('meetings')
       .update({ status: 'joining' })
       .eq('id', meetingId);
     
     // Call Koov API
     const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
     const response = await fetch(`${koovApiUrl}/api/join-meeting`, {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify({
         meeting_link: meeting.meeting_link,
         display_name: 'Koov',
       }),
     });
     
     if (!response.ok) {
       await supabase
         .from('meetings')
         .update({ status: 'failed' })
         .eq('id', meetingId);
       throw new Error('Failed to start Koov');
     }
     
     // Poll for status updates
     const maxAttempts = 30; // 60 seconds max
     for (let i = 0; i < maxAttempts; i++) {
       await new Promise(resolve => setTimeout(resolve, 2000));
       
       const statusResponse = await fetch(`${koovApiUrl}/api/status`);
       const koovStatus = await statusResponse.json();
       
       const statusMap: Record<string, string> = {
         'joining': 'joining',
         'active': 'live',
         'stopped': 'ended',
         'error': 'failed',
       };
       
       const supabaseStatus = statusMap[koovStatus.status] || 'joining';
       
       await supabase
         .from('meetings')
         .update({ status: supabaseStatus })
         .eq('id', meetingId);
       
       // Stop polling if active or stopped
       if (koovStatus.status === 'active' || koovStatus.status === 'stopped') {
         break;
       }
     }
   }
   ```

   **Option B: Client-side polling with React hook**
   ```typescript
   // hooks/useKoovStatus.ts
   'use client';
   
   import { useEffect, useState } from 'react';
   
   export function useKoovStatus(meetingId: string, enabled: boolean = true) {
     const [status, setStatus] = useState<any>(null);
     
     useEffect(() => {
       if (!enabled) return;
       
       const koovApiUrl = process.env.NEXT_PUBLIC_KOOV_API_URL || 'http://127.0.0.1:5000';
       
       const fetchStatus = async () => {
         const response = await fetch(`${koovApiUrl}/api/status`);
         const data = await response.json();
         setStatus(data);
         
         // Update Supabase
         if (data.status === 'active' || data.status === 'stopped') {
           await fetch(`/api/meetings/${meetingId}/update-status`, {
             method: 'PATCH',
             headers: { 'Content-Type': 'application/json' },
             body: JSON.stringify({
               status: data.status === 'active' ? 'live' : 'ended',
             }),
           });
         }
       };
       
       fetchStatus();
       const interval = setInterval(fetchStatus, 2000);
       
       return () => clearInterval(interval);
     }, [meetingId, enabled]);
     
     return status;
   }
   ```

4. Update the meeting details UI to:
   - Show "Joining" when status is `joining`
   - Show "Active" when status is `live` or `active`
   - Optionally show transcript chunks count and uptime

5. Add environment variable:
   - `KOOV_API_URL=http://127.0.0.1:5000` to `.env.local`

**Example code structure:**

```typescript
// app/api/koov/join/route.ts
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

**Please:**
1. Find my existing meeting start code (look for `triggerKoovSessionStart` or meeting start actions)
2. Create the `/api/koov/join` route
3. Integrate the Koov API call in the meeting start function
4. Add status polling to update Supabase when Koov becomes active
5. Update the UI to show "Active" status when Koov is working
6. Add proper error handling
7. Show me what files were changed

**Key Requirements:**
- Status must change from "Joining" to "Active" when Koov is actually listening and processing
- Poll status every 2 seconds until it becomes "active" or "stopped"
- Update Supabase meeting status accordingly
- Handle errors gracefully

---
