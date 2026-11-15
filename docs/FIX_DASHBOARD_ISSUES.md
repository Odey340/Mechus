# Fix Dashboard Integration Issues

## Issues Found in Logs

### 1. Selenium Not Installed ⚠️

**Error:**
```
WARNING koov.api_server: Selenium not installed. Install with: pip install selenium
```

**Fix:**
```bash
source .venv/bin/activate
pip install selenium
```

**Why:** Without Selenium, the API falls back to opening Safari instead of Chrome, which means:
- No auto-join functionality
- No Chrome profile (audio settings won't be saved)
- Manual clicking required every time

### 2. Transcript Endpoint 400 Errors ⚠️

**Error:**
```
GET /api/transcript HTTP/1.1" 400
```

**Cause:** The transcript endpoint now requires `meeting_id` parameter, but your dashboard is calling it without it.

**Fix in Dashboard:**
```typescript
// ❌ Wrong - Missing meeting_id
const response = await fetch('http://127.0.0.1:5000/api/transcript');

// ✅ Correct - Include meeting_id
const response = await fetch(`http://127.0.0.1:5000/api/transcript?meeting_id=${meetingId}`);
```

### 3. Stop Endpoint 500 Error ⚠️

**Error:**
```
POST /api/stop HTTP/1.1" 500
Error stopping session: 400 Bad Request: Failed to decode JSON object
```

**Cause:** The dashboard is calling `/api/stop` without a request body, but the endpoint expects JSON (even if empty).

**Fix in Dashboard:**
```typescript
// ❌ Wrong - No body
const response = await fetch('http://127.0.0.1:5000/api/stop', {
  method: 'POST',
});

// ✅ Correct - Include body with meeting_id
const response = await fetch('http://127.0.0.1:5000/api/stop', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    meeting_id: meetingId, // Use your meeting ID from Supabase
  }),
});
```

## Quick Fixes

### 1. Install Selenium
```bash
source .venv/bin/activate
pip install selenium
```

### 2. Update Dashboard API Calls

**Transcript endpoint:**
```typescript
// Get transcript for specific meeting
const response = await fetch(
  `http://127.0.0.1:5000/api/transcript?meeting_id=${meetingId}`
);
```

**Stop endpoint:**
```typescript
// Stop specific meeting
const response = await fetch('http://127.0.0.1:5000/api/stop', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ meeting_id: meetingId }),
});
```

**Status endpoint (already correct):**
```typescript
// Get status for specific meeting
const response = await fetch(
  `http://127.0.0.1:5000/api/status?meeting_id=${meetingId}`
);
```

## After Fixes

1. ✅ Selenium installed → Chrome will open instead of Safari
2. ✅ Transcript endpoint works → Dashboard can fetch transcripts
3. ✅ Stop endpoint works → Dashboard can stop meetings

## Test

After making these changes:

1. Restart the API server
2. Join a meeting from dashboard
3. Check status - should work
4. Get transcript - should work
5. Stop meeting - should work
