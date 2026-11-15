# Audio Setup (One-Time Manual Configuration)

## Important: Audio Devices Must Be Set Up Once

**Mechus cannot automatically configure audio devices.** You need to set this up **once** in your Zoom/Meet settings, and it will be remembered for future meetings.

## One-Time Setup Steps

### 1. Install BlackHole (if not done)

Follow instructions in `SETUP_MACOS.txt` to install BlackHole virtual audio device.

### 2. Configure Zoom/Meet Audio Settings (One-Time)

**For Zoom:**
1. Open Zoom → Settings → Audio
2. **Microphone:** Select `BlackHole 2ch` (or your configured input)
3. **Speaker:** Select your Multi-Output device (includes your speakers + BlackHole)
4. **Save** - These settings will be remembered

**For Google Meet:**
1. Join a test meeting
2. Click the three dots (⋮) → Settings → Audio
3. **Microphone:** Select `BlackHole 2ch`
4. **Speaker:** Select your Multi-Output device
5. **Save** - These settings will be remembered

### 3. Verify Mechus Configuration

In your `.env` file:
```
MECHUS_INPUT_DEVICE=BlackHole 2ch
MECHUS_OUTPUT_DEVICE=BlackHole 2ch
```

## After One-Time Setup

Once configured:
- ✅ Zoom/Meet will remember your audio settings
- ✅ Future meetings will use the correct devices automatically
- ✅ You only need to click "Join" (which we're automating)

## Current Limitations

**What Mechus CAN do:**
- ✅ Open meeting link automatically
- ✅ Auto-click "Join" button (with browser automation)
- ✅ Start listening automatically

**What Mechus CANNOT do:**
- ❌ Automatically set audio devices in Zoom/Meet (must be done manually once)
- ❌ Bypass browser security to control audio settings

## Troubleshooting

**Audio not working:**
- Check Zoom/Meet audio settings are saved
- Verify BlackHole is installed
- Check `.env` file has correct device names

**Join button not clicking:**
- Install Selenium: `pip install selenium`
- Install ChromeDriver (comes with Selenium 4.15+)
- Check browser console for errors
