# Cursor Prompt: Stop Koov When Meeting Ends

Copy and paste this into Cursor for your dashboard repo:

---

I need to stop Koov when a user clicks "End Meeting" or "Cancel Meeting" in the dashboard.

**Current situation:**
- Users can end meetings in the dashboard
- But Koov keeps running on the server
- I need Koov to stop when the meeting is ended

**What I need:**

1. When user clicks "End Meeting" or "Cancel Meeting":
   - Call Koov API: `POST http://127.0.0.1:5000/api/stop`
   - Update Supabase meeting status to `ended`
   - Show user feedback

2. The Koov API stop endpoint:
   ```
   POST http://127.0.0.1:5000/api/stop
   ```
   
   **Response:**
   ```json
   {
     "success": true,
     "message": "Koov session stopped",
     "meeting_link": "https://..."
   }
   ```

**Implementation:**

### Option 1: Add to Meeting End Function

Find where meetings are ended (look for "End Meeting" button or meeting end function):

```typescript
// In your meeting end/cancel function
async function endMeeting(meetingId: string) {
  const supabase = createClient();
  
  // 1. Update Supabase status
  await supabase
    .from('meetings')
    .update({ 
      status: 'ended',
      ended_at: new Date().toISOString()
    })
    .eq('id', meetingId);
  
  // 2. Stop Koov session
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  
  try {
    const response = await fetch(`${koovApiUrl}/api/stop`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
    });
    
    const data = await response.json();
    
    if (!response.ok) {
      console.error('Failed to stop Koov:', data.error);
      // Don't throw - meeting is already ended in Supabase
    } else {
      console.log('Koov stopped successfully:', data);
    }
  } catch (error) {
    console.error('Error stopping Koov:', error);
    // Don't throw - meeting is already ended in Supabase
  }
  
  return { success: true };
}
```

### Option 2: Create API Route

```typescript
// app/api/meetings/[id]/end/route.ts
import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  const meetingId = params.id;
  const supabase = createClient();
  const koovApiUrl = process.env.KOOV_API_URL || 'http://127.0.0.1:5000';
  
  try {
    // 1. Update Supabase
    const { error: updateError } = await supabase
      .from('meetings')
      .update({
        status: 'ended',
        ended_at: new Date().toISOString(),
      })
      .eq('id', meetingId);
    
    if (updateError) {
      throw updateError;
    }
    
    // 2. Stop Koov session
    const koovResponse = await fetch(`${koovApiUrl}/api/stop`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        meeting_id: meetingId, // Use your Supabase meeting ID
      }),
    });
    
    const koovData = await koovResponse.json();
    
    if (!koovResponse.ok) {
      console.error('Koov stop failed:', koovData.error);
      // Still return success since meeting is ended in Supabase
    }
    
    return NextResponse.json({
      success: true,
      meeting_id: meetingId,
      koov_stopped: koovResponse.ok,
    });
    
  } catch (error) {
    console.error('Error ending meeting:', error);
    return NextResponse.json(
      { error: 'Failed to end meeting' },
      { status: 500 }
    );
  }
}
```

Then call it from your component:

```typescript
// In your component
const handleEndMeeting = async () => {
  const response = await fetch(`/api/meetings/${meetingId}/end`, {
    method: 'POST',
  });
  
  if (response.ok) {
    // Meeting ended successfully
    router.push('/app/meetings');
  }
};
```

### Option 3: Update Cancel Meeting Button

Find your "Cancel Meeting" or "End Meeting" button component:

```typescript
// components/EndMeetingButton.tsx
'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';

export function EndMeetingButton({ meetingId }: { meetingId: string }) {
  const [loading, setLoading] = useState(false);
  const router = useRouter();
  
  const handleEnd = async () => {
    if (!confirm('Are you sure you want to end this meeting? Koov will stop.')) {
      return;
    }
    
    setLoading(true);
    
    try {
      // Call your end meeting API
      const response = await fetch(`/api/meetings/${meetingId}/end`, {
        method: 'POST',
      });
      
      if (!response.ok) {
        throw new Error('Failed to end meeting');
      }
      
      // Redirect to meetings list
      router.push('/app/meetings');
      router.refresh();
      
    } catch (error) {
      console.error('Error ending meeting:', error);
      alert('Failed to end meeting. Please try again.');
    } finally {
      setLoading(false);
    }
  };
  
  return (
    <button
      onClick={handleEnd}
      disabled={loading}
      className="px-4 py-2 bg-red-500 text-white rounded disabled:opacity-50"
    >
      {loading ? 'Ending...' : 'End Meeting'}
    </button>
  );
}
```

**Please:**
1. Find where meetings are ended in my codebase (look for "End Meeting" or "Cancel Meeting" buttons/functions)
2. Add call to Koov API `/api/stop` endpoint
3. Update Supabase meeting status to `ended`
4. Handle errors gracefully (meeting should still end even if Koov stop fails)
5. Show user feedback (loading state, success/error messages)
6. Show me what files were changed

**Key requirements:**
- Call `POST http://127.0.0.1:5000/api/stop` when meeting ends
- Update Supabase status to `ended`
- Don't fail if Koov stop fails (meeting should still end)
- Provide user feedback

---
