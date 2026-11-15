# Multiple Meetings Support

## ✅ Now Supported!

**Mechus now supports multiple concurrent meetings!**

The API server has been updated to:
1. ✅ Store sessions in a dictionary (keyed by `meeting_id`)
2. ✅ Each meeting gets its own Mechus agent instance
3. ✅ Each meeting has its own audio stream
4. ✅ Status and transcript endpoints work per-meeting

## How It Will Work

### One-Time Setup (Cloud)

**Yes, you set it up once in the cloud:**

1. **First meeting join:**
   - Browser opens (visible via VNC/X11 or with `MECHUS_SHOW_BROWSER=true`)
   - Join test meeting
   - Set audio devices to virtual devices (mechus_input/mechus_output)
   - Chrome saves these settings in the profile

2. **After setup:**
   - All future meetings use the saved audio settings automatically
   - No manual setup needed
   - Works for multiple meetings

### Multiple Meetings

**After code update:**
- ✅ Multiple meetings can run simultaneously
- ✅ Each meeting has its own Mechus agent
- ✅ Each meeting has its own transcript
- ✅ Status tracked per-meeting

## Implementation

The API now:
- ✅ Accepts `meeting_id` in requests (auto-generated if not provided)
- ✅ Stores sessions in a dictionary: `_active_sessions[meeting_id] = session`
- ✅ Each session is completely independent
- ✅ Status endpoint accepts `meeting_id` parameter
- ✅ Transcript endpoint requires `meeting_id` parameter

## Usage

**Start meeting:**
```json
POST /api/join-meeting
{
  "meeting_link": "https://...",
  "meeting_id": "meeting-123",  // Add this
  "display_name": "Mechus"
}
```

**Get status:**
```json
GET /api/status?meeting_id=meeting-123
```

**Stop meeting:**
```json
POST /api/stop
{
  "meeting_id": "meeting-123"
}
```

## Resource Considerations

**For multiple meetings, you need:**
- More CPU (each meeting processes audio)
- More RAM (each agent loads models)
- More bandwidth (audio streams)

**Recommended server sizes:**
- 1-2 meetings: 2 vCPU, 4 GB RAM
- 3-5 meetings: 4 vCPU, 8 GB RAM
- 5+ meetings: 8 vCPU, 16 GB RAM
