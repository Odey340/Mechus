# Supabase + Koov API Integration

This guide shows how to integrate your Supabase-based dashboard with the Koov API server.

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
│  Koov API Server │                                                    
│  (Flask)        │────────▶ Starts Koov Agent ────▶ Joins Meeting
└─────────────────┘                                                    
```

**Key Points:**
- **Dashboard** stores meetings in **Supabase** (your existing setup)
- **Dashboard** calls **Koov API Server** when starting a meeting
- **Koov API Server** doesn't need Supabase - it just needs the meeting link
- **Dashboard** can update Supabase with Koov session status

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
  
  // 3. Call Koov API Server to start the session
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  
  try {
    const response = await fetch(`${koovApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link: meeting.meeting_link,
        display_name: 'Koov',
      }),
    });
    
    const koovData = await response.json();
    
    if (!response.ok) {
      // If Koov fails, update Supabase status to "failed"
      await supabase
        .from('meetings')
        .update({
          status: 'failed',
          error: koovData.error || 'Failed to start Koov session',
        })
        .eq('id', meetingId);
      
      throw new Error(koovData.error || 'Failed to start Koov session');
    }
    
    // 4. Update Supabase with success status
    await supabase
      .from('meetings')
      .update({
        status: 'live', // or 'joining' if you want to wait for confirmation
        koov_session_started_at: new Date().toISOString(),
      })
      .eq('id', meetingId);
    
    return {
      success: true,
      meeting,
      koov: koovData,
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

This keeps the Koov API URL server-side:

```typescript
// app/api/meetings/[id]/start-koov/route.ts
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
    
    // Call Koov API
    const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
    
    const koovResponse = await fetch(`${koovApiUrl}/api/join-meeting`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_link: meeting.meeting_link,
        display_name: 'Koov',
      }),
    });
    
    const koovData = await koovResponse.json();
    
    if (!koovResponse.ok) {
      // Update Supabase on failure
      await supabase
        .from('meetings')
        .update({
          status: 'failed',
          error: koovData.error || 'Failed to start Koov',
        })
        .eq('id', meetingId);
      
      return NextResponse.json(koovData, { status: koovResponse.status });
    }
    
    // Update Supabase on success
    await supabase
      .from('meetings')
      .update({
        status: 'live',
        koov_session_started_at: new Date().toISOString(),
      })
      .eq('id', meetingId);
    
    return NextResponse.json({
      success: true,
      meeting_id: meetingId,
      koov: koovData,
    });
    
  } catch (error) {
    console.error('Error starting Koov session:', error);
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
      const response = await fetch(`/api/meetings/${meetingId}/start-koov`, {
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
        {loading ? 'Starting...' : 'Start Koov Meeting'}
      </button>
      {error && (
        <p className="text-red-500 mt-2">{error}</p>
      )}
    </div>
  );
}
```

### 4. Optional: Poll Koov Status and Update Supabase

You can periodically check Koov status and sync it to Supabase:

```typescript
// app/api/meetings/[id]/sync-koov-status/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  const meetingId = params.id;
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  const supabase = createClient();
  
  try {
    // Get Koov status
    const koovResponse = await fetch(`${koovApiUrl}/api/status`);
    const koovStatus = await koovResponse.json();
    
    // Update Supabase based on Koov status
    const statusMap: Record<string, string> = {
      'starting': 'joining',
      'running': 'live',
      'stopped': 'ended',
      'error': 'failed',
    };
    
    const supabaseStatus = statusMap[koovStatus.status] || meeting.status;
    
    await supabase
      .from('meetings')
      .update({
        status: supabaseStatus,
        koov_status: koovStatus.status,
        koov_uptime_seconds: koovStatus.uptime_seconds,
      })
      .eq('id', meetingId);
    
    return NextResponse.json({ success: true, koov_status: koovStatus });
    
  } catch (error) {
    return NextResponse.json(
      { error: 'Failed to sync status' },
      { status: 500 }
    );
  }
}
```

## Database Schema Addition (Optional)

If you want to track Koov-specific data in Supabase, add these columns to your `meetings` table:

```sql
ALTER TABLE meetings
ADD COLUMN IF NOT EXISTS koov_session_started_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS koov_status TEXT,
ADD COLUMN IF NOT EXISTS koov_uptime_seconds INTEGER;
```

## Environment Variables

Add to your Next.js `.env.local`:

```bash
# Koov API Server URL (running locally)
KOOV_API_URL=http://127.0.0.1:5000

# Your existing Supabase variables
NEXT_PUBLIC_SUPABASE_URL=...
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
```

## Complete Flow

1. **User clicks "Start Meeting"** in dashboard
2. **Dashboard** reads meeting from Supabase
3. **Dashboard** updates meeting status to `joining` in Supabase
4. **Dashboard** calls Koov API Server with meeting link
5. **Koov API Server** opens meeting in browser and starts Koov agent
6. **Dashboard** updates Supabase status to `live`
7. **Dashboard** can poll Koov status and sync to Supabase

## Key Benefits

✅ **Separation of Concerns**: Supabase handles data, Koov API handles voice agent  
✅ **No Direct Database Access Needed**: Koov API doesn't need Supabase credentials  
✅ **Status Sync**: Dashboard can keep Supabase in sync with Koov status  
✅ **Error Handling**: Failures update Supabase appropriately  

## Troubleshooting

**"Connection refused" to Koov API:**
- Make sure the Koov API server is running: `python3 scripts/start_api_server.py`
- Check the port matches your `KOOV_API_URL`

**Status not updating in Supabase:**
- Check your Supabase RLS policies allow updates
- Verify the meeting ID is correct
- Check browser console for errors
