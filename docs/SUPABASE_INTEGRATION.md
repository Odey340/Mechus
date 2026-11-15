# Supabase + Mechus API Integration

This guide shows how to integrate your Supabase-based dashboard with the Mechus API server.

## Architecture Overview

```
┌─────────────────┐         ┌──────────────┐         ┌─────────────┐
│   Dashboard     │────────▶│   Supabase   │         │             │
│   (Next.js)     │         │   Database   │         │             │
└─────────────────┘         └──────────────┘         └─────────────┘
         │                                                            
         │ HTTP Request                                                
         ▼                                                            
┌─────────────────┐                                                    
│  Mechus API Server │                                                    
│  (Flask)        │────────▶ Starts Mechus Agent ────▶ Joins Meeting
└─────────────────┘                                                    
```

**Key Points:**
- **Dashboard** stores meetings in **Supabase** (your existing setup)
- **Dashboard** calls **Mechus API Server** when starting a meeting
- **Mechus API Server** doesn't need Supabase - it just needs the meeting link
- **Dashboard** can update Supabase with Mechus session status

## Complete Integration Example

### 1. Update Your Meeting Start Function

In your dashboard's server action or API route where you start meetings:

```typescript
// lib/meetings/actions.ts or app/api/meetings/[id]/start/route.ts
import { createClient } from '@/lib/supabase/server';

export async function startMeeting(meetingId: string) {
  const supabase = createClient();
  
  // 1. Get meeting from Supabase
  const { data: meeting, error: fetchError } = await supabase
    .from('meetings')
    .select('*')
    .eq('id', meetingId)
    .single();
  
  if (fetchError || !meeting) {
    throw new Error('Meeting not found');
  }
  
  if (!meeting.meeting_link) {
    throw new Error('Meeting link is required');
  }
  
  // 2. Update meeting status in Supabase to "joining"
  const { error: updateError } = await supabase
    .from('meetings')
    .update({
      status: 'joining',
      started_at: new Date().toISOString(),
    })
    .eq('id', meetingId);
  
  if (updateError) {
    throw new Error('Failed to update meeting status');
  }
  
  // 3. Call Mechus API Server to start the session
  const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
  
  try {
    const response = await fetch(`${mechusApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link: meeting.meeting_link,
        display_name: 'Mechus',
      }),
    });
    
    const mechusData = await response.json();
    
    if (!response.ok) {
      // If Mechus fails, update Supabase status to "failed"
      await supabase
        .from('meetings')
        .update({
          status: 'failed',
          error: mechusData.error || 'Failed to start Mechus session',
        })
        .eq('id', meetingId);
      
      throw new Error(mechusData.error || 'Failed to start Mechus session');
    }
    
    // 4. Update Supabase with success status
    await supabase
      .from('meetings')
      .update({
        status: 'live', // or 'joining' if you want to wait for confirmation
        mechus_session_started_at: new Date().toISOString(),
      })
      .eq('id', meetingId);
    
    return {
      success: true,
      meeting,
      mechus: mechusData,
    };
    
  } catch (error) {
    // Update Supabase on error
    await supabase
      .from('meetings')
      .update({
        status: 'failed',
        error: error instanceof Error ? error.message : 'Unknown error',
      })
      .eq('id', meetingId);
    
    throw error;
  }
}
```

### 2. Create a Next.js API Route (Recommended)

This keeps the Mechus API URL server-side:

```typescript
// app/api/meetings/[id]/start-mechus/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  try {
    const meetingId = params.id;
    const supabase = createClient();
    
    // Get meeting from Supabase
    const { data: meeting, error: fetchError } = await supabase
      .from('meetings')
      .select('*')
      .eq('id', meetingId)
      .single();
    
    if (fetchError || !meeting) {
      return NextResponse.json(
        { error: 'Meeting not found' },
        { status: 404 }
      );
    }
    
    if (!meeting.meeting_link) {
      return NextResponse.json(
        { error: 'Meeting link is required' },
        { status: 400 }
      );
    }
    
    // Update status to "joining"
    await supabase
      .from('meetings')
      .update({
        status: 'joining',
        started_at: new Date().toISOString(),
      })
      .eq('id', meetingId);
    
    // Call Mechus API
    const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
    
    const mechusResponse = await fetch(`${mechusApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link: meeting.meeting_link,
        display_name: 'Mechus',
      }),
    });
    
    const mechusData = await mechusResponse.json();
    
    if (!mechusResponse.ok) {
      // Update Supabase on failure
      await supabase
        .from('meetings')
        .update({
          status: 'failed',
          error: mechusData.error || 'Failed to start Mechus',
        })
        .eq('id', meetingId);
      
      return NextResponse.json(mechusData, { status: mechusResponse.status });
    }
    
    // Update Supabase on success
    await supabase
      .from('meetings')
      .update({
        status: 'live',
        mechus_session_started_at: new Date().toISOString(),
      })
      .eq('id', meetingId);
    
    return NextResponse.json({
      success: true,
      meeting_id: meetingId,
      mechus: mechusData,
    });
    
  } catch (error) {
    console.error('Error starting Mechus session:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
```

### 3. Update Your Frontend Component

```typescript
// components/StartMeetingButton.tsx
'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';

export function StartMeetingButton({ meetingId }: { meetingId: string }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  
  const handleStart = async () => {
    setLoading(true);
    setError(null);
    
    try {
      const response = await fetch(`/api/meetings/${meetingId}/start-mechus`, {
        method: 'POST',
      });
      
      const data = await response.json();
      
      if (!response.ok) {
        throw new Error(data.error || 'Failed to start meeting');
      }
      
      // Refresh the page or update UI
      router.refresh();
      
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
        disabled={loading}
        className="px-4 py-2 bg-blue-500 text-white rounded"
      >
        {loading ? 'Starting...' : 'Start Mechus Meeting'}
      </button>
      {error && (
        <p className="text-red-500 mt-2">{error}</p>
      )}
    </div>
  );
}
```

### 4. Optional: Poll Mechus Status and Update Supabase

You can periodically check Mechus status and sync it to Supabase:

```typescript
// app/api/meetings/[id]/sync-mechus-status/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  const meetingId = params.id;
  const mechusApiUrl = process.env.MECHUS_API_URL || 'http://127.0.0.1:5000';
  const supabase = createClient();
  
  try {
    // Get Mechus status
    const mechusResponse = await fetch(`${mechusApiUrl}/api/status`);
    const mechusStatus = await mechusResponse.json();
    
    // Update Supabase based on Mechus status
    const statusMap: Record<string, string> = {
      'starting': 'joining',
      'running': 'live',
      'stopped': 'ended',
      'error': 'failed',
    };
    
    const supabaseStatus = statusMap[mechusStatus.status] || meeting.status;
    
    await supabase
      .from('meetings')
      .update({
        status: supabaseStatus,
        mechus_status: mechusStatus.status,
        mechus_uptime_seconds: mechusStatus.uptime_seconds,
      })
      .eq('id', meetingId);
    
    return NextResponse.json({ success: true, mechus_status: mechusStatus });
    
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to sync status' },
      { status: 500 }
    );
  }
}
```

## Database Schema Addition (Optional)

If you want to track Mechus-specific data in Supabase, add these columns to your `meetings` table:

```sql
ALTER TABLE meetings
ADD COLUMN IF NOT EXISTS mechus_session_started_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS mechus_status TEXT,
ADD COLUMN IF NOT EXISTS mechus_uptime_seconds INTEGER;
```

## Environment Variables

Add to your Next.js `.env.local`:

```bash
# Mechus API Server URL (running locally)
MECHUS_API_URL=http://127.0.0.1:5000

# Your existing Supabase variables
NEXT_PUBLIC_SUPABASE_URL=...
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
```

## Complete Flow

1. **User clicks "Start Meeting"** in dashboard
2. **Dashboard** reads meeting from Supabase
3. **Dashboard** updates meeting status to `joining` in Supabase
4. **Dashboard** calls Mechus API Server with meeting link
5. **Mechus API Server** opens meeting in browser and starts Mechus agent
6. **Dashboard** updates Supabase status to `live`
7. **Dashboard** can poll Mechus status and sync to Supabase

## Key Benefits

✅ **Separation of Concerns**: Supabase handles data, Mechus API handles voice agent  
✅ **No Direct Database Access Needed**: Mechus API doesn't need Supabase credentials  
✅ **Status Sync**: Dashboard can keep Supabase in sync with Mechus status  
✅ **Error Handling**: Failures update Supabase appropriately  

## Troubleshooting

**"Connection refused" to Mechus API:**
- Make sure the Mechus API server is running: `python3 scripts/start_api_server.py`
- Check the port matches your `MECHUS_API_URL`

**Status not updating in Supabase:**
- Check your Supabase RLS policies allow updates
- Verify the meeting ID is correct
- Check browser console for errors
