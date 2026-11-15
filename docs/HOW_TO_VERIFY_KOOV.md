# How to Verify Koov is Working

## Quick Checks

### 1. Check API Server is Running

**In the terminal where you started the API server**, you should see:
```
Starting Koov API server on 127.0.0.1:5000
 * Running on http://127.0.0.1:5000
```

**Test the API:**
```bash
curl http://127.0.0.1:5000/health
```

Should return:
```json
{"status":"ok","service":"koov-api"}
```

### 2. Check if Koov Agent is Active

**After joining a meeting**, look in the API server terminal for:
```
Koov dormant. Wake phrase: computer
```

This means Koov is listening and waiting for the wake word.

**Check status via API:**
```bash
curl http://127.0.0.1:5000/api/status
```

Should return something like:
```json
{
  "active": true,
  "status": "running",
  "meeting_link": "https://meet.google.com/...",
  "started_at": 1234567890.123,
  "uptime_seconds": 120.5
}
```

### 3. Test the Wake Word

**In the meeting:**
1. Say **"computer"** clearly
2. Wait 1-2 seconds
3. Ask a question like: **"computer, what did we just discuss?"**

**What to look for:**
- In the API server terminal, you should see:
  ```
  Wake detected
  Request: [your question]
  Response: [Koov's answer]
  ```
- Koov should speak its response in the meeting
- You should hear Koov's voice through the meeting

### 4. Check Audio Devices

**Verify audio routing is correct:**

1. **In Zoom/Meet settings:**
   - Microphone: `BlackHole 2ch` (or your configured input)
   - Speaker: Your Multi-Output device

2. **Check Koov's audio config:**
   - Look at your `.env` file:
     ```
     KOOV_INPUT_DEVICE=BlackHole 2ch
     KOOV_OUTPUT_DEVICE=BlackHole 2ch
     ```

3. **Test audio flow:**
   - Speak in the meeting → Should appear in API server logs
   - Koov responds → Should hear it in the meeting

## Detailed Verification Steps

### Step 1: API Server Health

```bash
# Check if server is responding
curl http://127.0.0.1:5000/health

# Check current session status
curl http://127.0.0.1:5000/api/status
```

### Step 2: Join a Meeting

1. **From your dashboard:**
   - Paste a meeting link
   - Click "Start" or "Join Meeting"
   - Meeting should open in browser automatically

2. **Check API server logs:**
   ```
   Opening meeting link: https://...
   Koov dormant. Wake phrase: computer
   ```

### Step 3: Test Wake Word Detection

**In the meeting, say:**
- "computer" (wait for response)
- "computer, summarize the last 5 minutes"
- "computer, what are the action items?"

**Expected behavior:**
- Terminal shows: `Wake detected`
- Terminal shows: `Request: [your question]`
- Terminal shows: `Response: [Koov's answer]`
- You hear Koov speaking in the meeting

### Step 4: Check for Errors

**Common issues and what to look for:**

1. **No wake word detection:**
   - Check: `Koov dormant. Wake phrase: computer` appears in logs
   - Check: Audio device is correct in `.env`
   - Check: Microphone in meeting is set to BlackHole

2. **Koov doesn't respond:**
   - Check: Terminal shows "Wake detected"
   - Check: Request text appears in logs
   - Check: Response text appears in logs
   - Check: TTS (ElevenLabs) is configured correctly

3. **Can't hear Koov:**
   - Check: Speaker in meeting is set correctly
   - Check: `KOOV_OUTPUT_DEVICE` in `.env`
   - Check: Audio output routing (Multi-Output device)

## Visual Indicators

### ✅ Koov is Working When:

- ✅ API server shows: `Koov dormant. Wake phrase: computer`
- ✅ Saying "computer" triggers: `Wake detected` in logs
- ✅ Questions get responses in the terminal
- ✅ You hear Koov speaking in the meeting
- ✅ Status API shows: `"status": "running"`

### ❌ Koov is NOT Working When:

- ❌ No "Koov dormant" message after joining meeting
- ❌ Saying "computer" doesn't trigger anything
- ❌ No audio in meeting (can't hear Koov)
- ❌ Status API shows: `"status": "stopped"` or `"active": false`
- ❌ Error messages in terminal

## Quick Test Script

Save this as `test_koov.sh`:

```bash
#!/bin/bash

echo "1. Testing API server health..."
curl -s http://127.0.0.1:5000/health | jq .

echo -e "\n2. Checking Koov status..."
curl -s http://127.0.0.1:5000/api/status | jq .

echo -e "\n3. If status shows 'active: true', Koov is running!"
echo "   Try saying 'computer' in your meeting to test wake word."
```

Run it:
```bash
chmod +x test_koov.sh
./test_koov.sh
```

## Troubleshooting Checklist

- [ ] API server is running (`http://127.0.0.1:5000` responds)
- [ ] Meeting link was opened successfully
- [ ] Terminal shows "Koov dormant. Wake phrase: computer"
- [ ] Audio devices are configured correctly in `.env`
- [ ] Zoom/Meet microphone is set to BlackHole
- [ ] Zoom/Meet speaker is set correctly
- [ ] Wake word "computer" is detected (see logs)
- [ ] Koov responds to questions (see logs)
- [ ] You can hear Koov in the meeting

## What Success Looks Like

**Terminal output when working:**
```
Starting Koov API server on 127.0.0.1:5000
 * Running on http://127.0.0.1:5000
Opening meeting link: https://meet.google.com/...
Koov dormant. Wake phrase: computer
Wake detected
Request: summarize the last 5 minutes
Response: Here's a quick recap: [summary text]
Returned dormant
```

**In the meeting:**
- You hear Koov's voice responding
- Other participants can hear Koov
- Koov understands and responds to questions
