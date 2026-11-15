# Dashboard Integration Example

This file shows how to integrate the Koov API server with your Next.js dashboard.

## Update `triggerKoovSessionStart` Function

Replace the placeholder function in your dashboard code with this implementation:

```typescript
// lib/meetings/actions.ts or similar
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

## Create API Route (Optional)

If you prefer to call the Koov API through your Next.js backend:

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

Then update `triggerKoovSessionStart`:

```typescript
async function triggerKoovSessionStart(meeting: any): Promise<void> {
  const meetingLink = meeting.meeting_link;
  
  if (!meetingLink) {
    throw new Error('Meeting link is required');
  }
  
  const response = await fetch('/api/koov/join', {
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
}
```

## Environment Variables

Add to your `.env.local` (Next.js):

```bash
KOOV_API_URL=http://127.0.0.1:5000
```

## Status Monitoring

You can also add status monitoring to your dashboard:

```typescript
// hooks/useKoovStatus.ts
'use client';

import { useEffect, useState } from 'react';

export function useKoovStatus() {
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  
  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const koovApiUrl = process.env.NEXT_PUBLIC_KOOV_API_URL || 'http://127.0.0.1:5000';
        const response = await fetch(`${koovApiUrl}/api/status`);
        const data = await response.json();
        setStatus(data);
      } catch (error) {
        console.error('Failed to fetch Koov status:', error);
      } finally {
        setLoading(false);
      }
    };
    
    fetchStatus();
    const interval = setInterval(fetchStatus, 5000); // Poll every 5 seconds
    
    return () => clearInterval(interval);
  }, []);
  
  return { status, loading };
}
```

## Usage in Component

```typescript
// components/MeetingControl.tsx
'use client';

import { useKoovStatus } from '@/hooks/useKoovStatus';

export function MeetingControl({ meetingLink }: { meetingLink: string }) {
  const { status, loading } = useKoovStatus();
  
  const handleStart = async () => {
    const response = await fetch('/api/koov/join', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meeting_link: meetingLink }),
    });
    // Handle response
  };
  
  const handleStop = async () => {
    const koovApiUrl = process.env.NEXT_PUBLIC_KOOV_API_URL || 'http://127.0.0.1:5000';
    await fetch(`${koovApiUrl}/api/stop`, { method: 'POST' });
  };
  
  return (
    <div>
      {status?.active ? (
        <div>
          <p>Koov is active: {status.status}</p>
          <button onClick={handleStop}>Stop Koov</button>
        </div>
      ) : (
        <button onClick={handleStart}>Start Koov</button>
      )}
    </div>
  );
}
```

## Important Notes

1. **CORS**: The Koov API server has CORS enabled, but if you're accessing from a different origin, you may need to configure it.

2. **Error Handling**: Always handle errors gracefully. The Koov API may not be running, or there might be network issues.

3. **Status Polling**: Consider using WebSockets or Server-Sent Events for real-time status updates instead of polling.

4. **Security**: In production, consider adding authentication to the Koov API server or running it behind a reverse proxy.
