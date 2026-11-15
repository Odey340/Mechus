# One Meeting Per User Limit

## Overview

Mechus now enforces a **one active meeting per user** limit. This means:
- ✅ Each user can only have **one active meeting** at a time
- ✅ Multiple users can have meetings running simultaneously
- ✅ If a user tries to start a second meeting, they'll get an error

## How It Works

### API Changes

**`POST /api/join-meeting`** now requires `user_id`:

```json
{
  "meeting_link": "https://zoom.us/j/123456789",
  "user_id": "user-123",  // REQUIRED - from your Supabase auth
  "meeting_id": "meeting-123",  // optional
  "display_name": "Mechus"  // optional
}
```

**Response if user already has active meeting:**
```json
{
  "success": false,
  "error": "User user-123 already has an active meeting",
  "existing_meeting_id": "meeting-456",
  "existing_meeting_link": "https://zoom.us/j/987654321",
  "existing_status": "active"
}
```

### Status Endpoint

**`GET /api/status?user_id=user-123`** - Get all meetings for a specific user:

```json
{
  "user_id": "user-123",
  "sessions": [
    {
      "meeting_id": "meeting-456",
      "active": true,
      "status": "active",
      "meeting_link": "https://zoom.us/j/987654321",
      "started_at": 1234567890.123,
      "uptime_seconds": 120.5
    }
  ],
  "total_active": 1
}
```

## Dashboard Integration

### 1. Get User ID from Supabase Auth

```typescript
import { createClient } from '@/lib/supabase/client';

const supabase = createClient();
const { data: { user } } = await supabase.auth.getUser();
const userId = user?.id;
```

### 2. Update Join Meeting Call

```typescript
async function triggerMechusSessionStart(meeting: any, userId: string): Promise<void> {
  const response = await fetch('/api/mechus/join', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      meeting_link: meeting.meeting_link,
      meeting_id: meeting.id,
      user_id: userId,  // Add this
      display_name: 'Mechus',
    }),
  });
  
  if (!response.ok) {
    const error = await response.json();
    
    // Handle "user already has active meeting" error
    if (error.error?.includes('already has an active meeting')) {
      // Show user-friendly message
      throw new Error('You already have an active meeting. Please end it first.');
    }
    
    throw new Error(error.error || `HTTP ${response.status}`);
  }
  
  const data = await response.json();
  console.log('Mechus session started:', data);
}
```

### 3. Check User's Active Meeting Before Starting

```typescript
async function checkUserActiveMeeting(userId: string): Promise<boolean> {
  const response = await fetch(`http://127.0.0.1:5000/api/status?user_id=${userId}`);
  const data = await response.json();
  
  return data.total_active > 0;
}

// Before starting a meeting
const hasActiveMeeting = await checkUserActiveMeeting(userId);
if (hasActiveMeeting) {
  // Show warning or prevent starting
  alert('You already have an active meeting. Please end it first.');
  return;
}
```

### 4. Handle Error in UI

```typescript
try {
  await triggerMechusSessionStart(meeting, userId);
} catch (error) {
  if (error.message.includes('already has an active meeting')) {
    // Show user-friendly error
    toast.error('You already have an active meeting. Please end it first.');
  } else {
    toast.error('Failed to start meeting: ' + error.message);
  }
}
```

## Behavior

### Scenario 1: User Starts First Meeting
- ✅ Meeting starts normally
- ✅ Status: `joining` → `active`

### Scenario 2: User Tries to Start Second Meeting
- ❌ API returns `409 Conflict`
- ❌ Error message: "User {user_id} already has an active meeting"
- ✅ Dashboard should show error and prevent starting

### Scenario 3: User Ends Meeting, Then Starts New One
- ✅ First meeting ends
- ✅ User can now start a new meeting
- ✅ No conflicts

### Scenario 4: Multiple Users
- ✅ User A can have 1 meeting
- ✅ User B can have 1 meeting
- ✅ Both can run simultaneously
- ✅ No conflicts between users

## Benefits

1. **Resource Management**: Prevents users from accidentally starting multiple meetings
2. **Cost Control**: Limits resource usage per user
3. **Clear UX**: Users know they can only have one active meeting
4. **Error Prevention**: Catches issues before they happen

## Migration

If you have existing meetings without `user_id`, you'll need to:
1. Stop all existing meetings
2. Update dashboard to always send `user_id`
3. Restart meetings with `user_id` included

The API will reject requests without `user_id` going forward.
