# Cursor Prompt: Status Polling Only

Copy and paste this into Cursor for your dashboard repo:

---

I need to add status polling to update my meeting status from "Joining" to "Active" when Mechus is actually working.

**Current situation:**
- My dashboard shows "Joining" when a meeting starts
- I need it to change to "Active" when Mechus is actually listening and processing audio/transcripts
- Mechus API server is running on `http://127.0.0.1:5000`

**What I need:**

1. **Poll the Mechus status endpoint** (`GET http://127.0.0.1:5000/api/status`) every 2 seconds
2. **Update the Supabase meeting status** based on Mechus's status:
   - `joining` → Keep as `joining` in Supabase
   - `active` → Update Supabase to `live` (or `active` if that's your status)
   - `stopped` → Update Supabase to `ended`
   - `error` → Update Supabase to `failed`
3. **Stop polling** when status becomes `active` or `stopped`

**The Mechus API status response:**
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

**Implementation options:**

**Option 1: Server-side polling in meeting start function**
```typescript
async function startMeeting(meetingId: string) {
  // ... existing code to call Mechus API ...
  
  // After calling Mechus API, poll for status
  const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
  const maxAttempts = 30; // Poll for up to 60 seconds
  
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(resolve => setTimeout(resolve, 2000)); // Wait 2 seconds
    
    const statusResponse = await fetch(`${mechusApiUrl}/api/status`);
    const mechusStatus = await statusResponse.json();
    
    const statusMap: Record<string, string> = {
      'joining': 'joining',
      'active': 'live',  // or 'active' if that's your status
      'stopped': 'ended',
      'error': 'failed',
    };
    
    const supabaseStatus = statusMap[mechusStatus.status] || 'joining';
    
    await supabase
      .from('meetings')
      .update({ status: supabaseStatus })
      .eq('id', meetingId);
    
    // Stop polling if active or stopped
    if (mechusStatus.status === 'active' || mechusStatus.status === 'stopped') {
      break;
    }
  }
}
```

**Option 2: Client-side polling with React hook**
```typescript
// hooks/useMechusStatus.ts
'use client';

import { useEffect, useState } from 'react';

export function useMechusStatus(meetingId: string, enabled: boolean = true) {
  const [status, setStatus] = useState<any>(null);
  
  useEffect(() => {
    if (!enabled) return;
    
    const mechusApiUrl = process.env.NEXT_PUBLIC_MECHUS_API_URL || 'http://127.0.0.1:5000';
    
    const fetchStatus = async () => {
      try {
        const response = await fetch(`${mechusApiUrl}/api/status`);
        const data = await response.json();
        setStatus(data);
        
        // Update Supabase when status changes
        if (data.status === 'active' || data.status === 'stopped') {
          await fetch(`/api/meetings/${meetingId}/update-status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              status: data.status === 'active' ? 'live' : 'ended',
            }),
          });
        }
      } catch (error) {
        console.error('Failed to fetch Mechus status:', error);
      }
    };
    
    fetchStatus();
    const interval = setInterval(fetchStatus, 2000); // Poll every 2 seconds
    
    return () => clearInterval(interval);
  }, [meetingId, enabled]);
  
  return status;
}
```

Then use it in your component:
```typescript
// In your meeting details component
const mechusStatus = useMechusStatus(meetingId, true);

// Update UI based on status
const displayStatus = mechusStatus?.status === 'active' ? 'Active' : 
                     mechusStatus?.status === 'joining' ? 'Joining' : 
                     'Inactive';
```

**Please:**
1. Find where I'm starting meetings (look for meeting start function or `triggerMechusSessionStart`)
2. Add status polling after the Mechus API is called
3. Update Supabase meeting status when Mechus status becomes `active`
4. Update the UI to show "Active" when status is `active`
5. Stop polling when status is `active` or `stopped`
6. Show me what files were changed

**Key requirements:**
- Poll every 2 seconds
- Update Supabase status from `joining` to `live` (or `active`) when Mechus status is `active`
- Update UI to show "Active" status
- Stop polling once status is `active` or `stopped`

---
