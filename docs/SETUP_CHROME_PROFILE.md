# Setting Up Chrome Profile for Audio Automation

## One-Time Setup: Configure Audio in Chrome Profile

To enable full automation (including audio), you need to set up a Chrome profile once with the correct audio devices.

## Step-by-Step Setup

### 1. First Run: Set Up Audio Devices

**Temporary: Show browser for setup**

Add to your `.env`:
```bash
MECHUS_SHOW_BROWSER=true
```

**Run Mechus and join a test meeting:**
1. Start API server
2. Join a test meeting from dashboard
3. Browser will open (visible)
4. **In the meeting:**
   - Click audio settings
   - Set Microphone: `BlackHole 2ch`
   - Set Speaker: Your Multi-Output device
   - Save settings
5. Chrome will remember these settings in the profile

### 2. After Setup: Hide Browser

Change `.env`:
```bash
MECHUS_SHOW_BROWSER=false
```

Now browser runs invisibly but uses the saved audio settings.

## How It Works

1. **First time:** Browser opens, you set audio devices once
2. **Chrome saves** audio device preferences in the profile
3. **Future meetings:** Browser uses saved audio settings automatically
4. **No manual setup needed** after the first time

## Profile Location

Mechus creates a Chrome profile at:
```
~/.mechus-chrome-profile
```

This profile stores:
- Audio device preferences
- Meeting platform settings
- Other browser preferences

## Custom Profile Path

If you want to use a different profile:

```bash
MECHUS_CHROME_PROFILE_PATH=/path/to/your/profile
```

## Troubleshooting

**Audio not working:**
1. Set `MECHUS_SHOW_BROWSER=true`
2. Join a test meeting
3. Verify audio devices are set correctly
4. Chrome will save the settings
5. Set `MECHUS_SHOW_BROWSER=false` again

**Settings not saving:**
- Make sure you actually join the meeting (not just open the link)
- Set audio devices while in the meeting
- Wait a few seconds for Chrome to save

## Alternative: Use Zoom SDK

For complete programmatic control (no browser at all), use Zoom SDK:

1. Get Zoom SDK credentials
2. Use Zoom SDK to join meetings
3. Set audio devices via SDK
4. No browser needed

See `FULL_AUTOMATION_AUDIO.md` for Zoom SDK setup.
