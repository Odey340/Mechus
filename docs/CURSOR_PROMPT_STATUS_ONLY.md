# Cursor Prompt: Status Polling Only

Copy and paste this into Cursor for your dashboard repo:

---

I need to add status polling to update my meeting status from "Joining" to "Active" when Koov is actually working.

**Current situation:**
- My dashboard shows "Joining" when a meeting starts
- I need it to change to "Active" when Koov is actually listening and processing audio/transcripts
- Koov API server is running on `http://127.0.0.1:5000`

**What I need:**

1. **Poll the Koov status endpoint** (`GET http://127.0.0.1:5000/api/status`) every 2 seconds
2. **Update the Supabase meeting status** based on Koov's status:
   - `joining` → Keep as `joining` in Supabase
   - `active` → Update Supabase to `live` (or `active` if that's your status)
   - `stopped` → Update Supabase to `ended`
   - `error` → Update Supabase to `failed`
3. **Stop polling** when status becomes `active` or `stopped`

**The Koov API status response:**
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
  // ... existing code to call Koov API ...
  
  // After calling Koov API, poll for status
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  const maxAttempts = 30; // Poll for up to 60 seconds
  
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(resolve => setTimeout(resolve, 2000)); // Wait 2 seconds
    
    const statusResponse = await fetch(`${koovApiUrl}/api/status`);
    const koovStatus = await statusResponse.json();
    
    const statusMap: Record<string, string> = {
      'joining': 'joining',
      'active': 'live',  // or 'active' if that's your status
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

**Option 2: Client-side polling with React hook**
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
      try {
        const response = await fetch(`${koovApiUrl}/api/status`);
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
        console.error('Failed to fetch Koov status:', error);
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
const koovStatus = useKoovStatus(meetingId, true);

// Update UI based on status
const displayStatus = koovStatus?.status === 'active' ? 'Active' : 
                     koovStatus?.status === 'joining' ? 'Joining' : 
                     'Inactive';
```

**Please:**
1. Find where I'm starting meetings (look for meeting start function or `triggerKoovSessionStart`)
2. Add status polling after the Koov API is called
3. Update Supabase meeting status when Koov status becomes `active`
4. Update the UI to show "Active" when status is `active`
5. Stop polling when status is `active` or `stopped`
6. Show me what files were changed

**Key requirements:**
- Poll every 2 seconds
- Update Supabase status from `joining` to `live` (or `active`) when Koov status is `active`
- Update UI to show "Active" status
- Stop polling once status is `active` or `stopped`

---
