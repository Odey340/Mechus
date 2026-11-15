# Dashboard Status Polling Guide

This guide shows how to poll the Mechus API status and update your dashboard from "Joining" to "Active" when Mechus is actually working.

## Status Flow

```
"joining" → Mechus API called, meeting opening
    ↓
"active" → Mechus is listening and processing audio/transcripts
    ↓
"stopped" → Mechus session ended
```

## Updated API Status Response

The `/api/status` endpoint now returns:

```json
{
  "active": true,
  "status": "active",  // "joining", "active", "stopped", "error"
  "meeting_link": "https://meet.google.com/...",
  "started_at": 1234567890.123,
  "uptime_seconds": 120.5,
  "transcript_chunks": 5,  // Number of transcript chunks processed
  "last_activity": 1234568000.123  // Timestamp of last activity
}
```

## Implementation: Poll Status in Dashboard

### Option 1: Poll Status After Starting Meeting

Update your meeting start function to poll for status updates:

```typescript
// lib/meetings/actions.ts or similar
import { createClient } from '@/lib/supabase/server';

export async function startMeeting(meetingId: string) {
  const supabase = createClient();
  
  // 1. Get meeting from Supabase
  const { data: meeting } = await supabase
    .from('meetings')
    .select('*')
    .eq('id', meetingId)
    .single();
  
  // 2. Update status to "joining"
  await supabase
    .from('meetings')
    .update({ status: 'joining' })
    .eq('id', meetingId);
  
  // 3. Call Mechus API
  const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
  const response = await fetch(`${mechusApiUrl}/api/join-meeting`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      meeting_link: meeting.meeting_link,
      display_name: 'Mechus',
    }),
  });
  
  if (!response.ok) {
    await supabase
      .from('meetings')
      .update({ status: 'failed' })
      .eq('id', meetingId);
    throw new Error('Failed to start Mechus');
  }
  
  // 4. Poll for status updates
  await pollMechusStatus(meetingId, mechusApiUrl);
  
  return { success: true };
}

async function pollMechusStatus(meetingId: string, mechusApiUrl: string) {
  const supabase = createClient();
  const maxAttempts = 30; // Poll for up to 60 seconds (2s intervals)
  let attempts = 0;
  
  const poll = async (): Promise<void> => {
    attempts++;
    
    try {
      const response = await fetch(`${mechusApiUrl}/api/status`);
      const status = await response.json();
      
      // Update Supabase based on Mechus status
      const statusMap: Record<string, string> = {
        'joining': 'joining',
        'active': 'live',  // or 'active' if that's your status
        'stopped': 'ended',
        'error': 'failed',
      };
      
      const supabaseStatus = statusMap[status.status] || 'joining';
      
      await supabase
        .from('meetings')
        .update({ 
          status: supabaseStatus,
          // Optionally store additional info
          mechus_status: status.status,
          mechus_transcript_chunks: status.transcript_chunks,
        })
        .eq('id', meetingId);
      
      // If active, we're done polling
      if (status.status === 'active') {
        return;
      }
      
      // If stopped or error, we're done
      if (status.status === 'stopped' || status.status === 'error') {
        return;
      }
      
      // Continue polling if still joining
      if (attempts < maxAttempts && status.status === 'joining') {
        await new Promise(resolve => setTimeout(resolve, 2000)); // Wait 2 seconds
        return poll();
      }
      
    } catch (error) {
      console.error('Error polling Mechus status:', error);
      // Continue polling on error (might be temporary)
      if (attempts < maxAttempts) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        return poll();
      }
    }
  };
  
  await poll();
}
```

### Option 2: Use a React Hook for Real-time Status

Create a hook that polls status:

```typescript
// hooks/useMechusStatus.ts
'use client';

import { useEffect, useState } from 'react';

interface MechusStatus {
  active: boolean;
  status: 'joining' | 'active' | 'stopped' | 'error';
  meeting_link?: string;
  transcript_chunks?: number;
  uptime_seconds?: number;
}

export function useMechusStatus(meetingId: string, enabled: boolean = true) {
  const [status, setStatus] = useState<MechusStatus | null>(null);
  const [loading, setLoading] = useState(true);
  
  useEffect(() => {
    if (!enabled) return;
    
    const mechusApiUrl = process.env.NEXT_PUBLIC_MECHUS_API_URL || 'http://127.0.0.1:5000';
    
    const fetchStatus = async () => {
      try {
        const response = await fetch(`${mechusApiUrl}/api/status`);
        const data = await response.json();
        setStatus(data);
        
        // Update Supabase if status changed
        if (data.status === 'active' || data.status === 'stopped') {
          // Call your API route to update Supabase
          await fetch(`/api/meetings/${meetingId}/update-status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
              status: data.status === 'active' ? 'live' : 'ended',
              mechus_status: data.status,
            }),
          });
        }
      } catch (error) {
        console.error('Failed to fetch Mechus status:', error);
      } finally {
        setLoading(false);
      }
    };
    
    fetchStatus();
    
    // Poll every 2 seconds
    const interval = setInterval(fetchStatus, 2000);
    
    return () => clearInterval(interval);
  }, [meetingId, enabled]);
  
  return { status, loading };
}
```

Use in your component:

```typescript
// components/MeetingDetails.tsx
'use client';

import { useMechusStatus } from '@/hooks/useMechusStatus';

export function MeetingDetails({ meetingId }: { meetingId: string }) {
  const { status, loading } = useMechusStatus(meetingId, true);
  
  const displayStatus = status?.status === 'active' ? 'Active' : 
                       status?.status === 'joining' ? 'Joining' : 
                       'Inactive';
  
  return (
    <div>
      <div>Status: {displayStatus}</div>
      {status?.status === 'active' && (
        <div>
          <p>Mechus is listening and processing</p>
          <p>Transcript chunks: {status.transcript_chunks || 0}</p>
          <p>Uptime: {Math.floor(status.uptime_seconds || 0)}s</p>
        </div>
      )}
    </div>
  );
}
```

### Option 3: Server Action with Status Updates

Create a server action that updates status:

```typescript
// app/api/meetings/[id]/sync-status/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function GET(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  const meetingId = params.id;
  const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
  const supabase = createClient();
  
  try {
    // Get Mechus status
    const response = await fetch(`${mechusApiUrl}/api/status`);
    const mechusStatus = await response.json();
    
    // Map Mechus status to Supabase status
    const statusMap: Record<string, string> = {
      'joining': 'joining',
      'active': 'live',
      'stopped': 'ended',
      'error': 'failed',
    };
    
    const supabaseStatus = statusMap[mechusStatus.status] || 'joining';
    
    // Update Supabase
    await supabase
      .from('meetings')
      .update({
        status: supabaseStatus,
        mechus_status: mechusStatus.status,
        mechus_transcript_chunks: mechusStatus.transcript_chunks,
      })
      .eq('id', meetingId);
    
    return NextResponse.json({
      success: true,
      status: supabaseStatus,
      mechus: mechusStatus,
    });
    
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to sync status' },
      { status: 500 }
    );
  }
}
```

Then poll from frontend:

```typescript
// Poll every 2 seconds
useEffect(() => {
  const interval = setInterval(async () => {
    await fetch(`/api/meetings/${meetingId}/sync-status`);
  }, 2000);
  
  return () => clearInterval(interval);
}, [meetingId]);
```

## Status Mapping

Map Mechus API status to your dashboard status:

| Mechus API Status | `mechus_presence` | Dashboard Status | Description |
|----------------|-----------------|------------------|-------------|
| `joining` | `connecting` or `waiting_room` | `joining` | SDK connecting or in Zoom waiting room |
| `active` | `in_call` | `live` | Mechus is in the meeting (`bot_in_call: true`) |
| `stopped` | `left` | `ended` | Mechus session ended |
| `error` | `failed` | `failed` | Join failed — show `failure_reason` / `worker_error` |

**Important:** Poll `GET /api/status?meeting_id=<mechus_meeting_id>` (the `meeting_id` returned from join). Do **not** mark the meeting `live` when join returns `success: true` — wait until `mechus_presence === "in_call"` or `status === "active"`. For org Zoom links (`osu.zoom.us`, etc.), pass `zoom_access_token` in the join body or join fails with error 8 and never reaches the waiting room.

## When Status Changes to "Active"

The status changes to "active" when:
- ✅ Mechus's audio input is running (listening)
- ✅ Agent thread is alive
- ✅ At least 5 seconds have passed (initialization time)

You can also check `transcript_chunks > 0` to confirm Mechus is actually processing audio.

## Example: Complete Flow

1. User clicks "Start Meeting"
2. Dashboard updates status to `joining`
3. Dashboard calls Mechus API `/api/join-meeting`
4. Dashboard starts polling `/api/status` every 2 seconds
5. When status becomes `active`, update dashboard to `live` or `active`
6. Continue polling to show transcript chunks and activity

## Testing

Test the status polling:

```bash
# Start a meeting
curl -X POST http://127.0.0.1:5000/api/join-meeting \
  -H "Content-Type: application/json" \
  -d '{"meeting_link": "https://meet.google.com/test"}'

# Check status (should show "joining" then "active")
curl http://127.0.0.1:5000/api/status
```
