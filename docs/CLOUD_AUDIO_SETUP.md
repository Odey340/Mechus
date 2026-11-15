# How Mechus Works in the Cloud

## The Challenge

In the cloud:
- ❌ No physical audio devices
- ❌ No direct access to meeting audio
- ✅ Virtual audio devices (PulseAudio)
- ✅ Browser automation still works
- ✅ Chrome profile approach still works

## How Audio Works in Cloud

### Current Setup (Local)
```
Meeting → Browser → BlackHole → Mechus listens
Mechus speaks → BlackHole → Browser → Meeting
```

### Cloud Setup
```
Meeting → Browser (in cloud) → Virtual Audio → Mechus listens
Mechus speaks → Virtual Audio → Browser (in cloud) → Meeting
```

## Solution: Virtual Audio + Browser Automation

### 1. Virtual Audio Devices (PulseAudio)

On the cloud server, we create virtual audio devices:

```bash
# Create virtual audio sinks
pactl load-module module-null-sink sink_name=mechus_input
pactl load-module module-null-sink sink_name=mechus_output
```

### 2. Browser Automation (Same as Local)

- Browser runs in headless mode
- Uses Chrome profile with saved audio settings
- Auto-clicks "Join"
- Audio flows through virtual devices

### 3. Audio Flow in Cloud

```
Meeting Audio → Browser → PulseAudio (mechus_input) → Mechus processes
Mechus Response → PulseAudio (mechus_output) → Browser → Meeting
```

## Setup for Cloud Deployment

### Step 1: Set Up Virtual Audio on Server

```bash
# Install PulseAudio
sudo apt install -y pulseaudio pulseaudio-utils

# Create virtual audio devices
pactl load-module module-null-sink sink_name=mechus_input
pactl load-module module-null-sink sink_name=mechus_output

# Make persistent (add to /etc/pulse/default.pa)
echo 'load-module module-null-sink sink_name=mechus_input' | sudo tee -a /etc/pulse/default.pa
echo 'load-module module-null-sink sink_name=mechus_output' | sudo tee -a /etc/pulse/default.pa
```

### Step 2: Configure Mechus

In `.env` on cloud server:
```bash
MECHUS_INPUT_DEVICE=mechus_input
MECHUS_OUTPUT_DEVICE=mechus_output
```

### Step 3: Set Up Chrome Profile (One-Time)

**On the cloud server:**

1. **First, show browser for setup:**
   ```bash
   export MECHUS_SHOW_BROWSER=true
   ```

2. **Join a test meeting:**
   - Browser opens (you can VNC/SSH with X11 forwarding to see it)
   - Or use Xvfb (virtual display) for headless
   - Set audio devices in Zoom/Meet to use virtual audio

3. **Hide browser:**
   ```bash
   export MECHUS_SHOW_BROWSER=false
   ```

### Step 4: Use Xvfb for Headless Display (Recommended)

Since cloud servers don't have displays:

```bash
# Install Xvfb (virtual display)
sudo apt install -y xvfb

# Run Chrome with virtual display
xvfb-run -a python3 scripts/start_api_server.py
```

Or set in code:
```python
chrome_options.add_argument('--display=:99')
# Xvfb runs on display :99
```

## Complete Cloud Setup Script

```bash
#!/bin/bash
# setup_cloud_audio.sh

# 1. Install dependencies
sudo apt update
sudo apt install -y pulseaudio pulseaudio-utils xvfb

# 2. Create virtual audio devices
pactl load-module module-null-sink sink_name=mechus_input
pactl load-module module-null-sink sink_name=mechus_output

# 3. Make persistent
echo 'load-module module-null-sink sink_name=mechus_input' | sudo tee -a /etc/pulse/default.pa
echo 'load-module module-null-sink sink_name=mechus_output' | sudo tee -a /etc/pulse/default.pa

# 4. Start PulseAudio
pulseaudio --start

# 5. Verify audio devices
pactl list sinks | grep -A 5 mechus

# 6. Set up Xvfb for headless display
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 &
```

## How It Works in Cloud

### Meeting Join Flow

1. **Dashboard calls API** → Cloud server receives request
2. **Browser opens** (headless, via Xvfb)
3. **Auto-clicks Join** (Selenium automation)
4. **Audio flows** through virtual PulseAudio devices
5. **Mechus processes** audio from virtual device
6. **Mechus responds** through virtual device to browser
7. **Browser sends** audio to meeting

### Audio Device Flow

```
Meeting Platform (Zoom/Meet)
    ↓ (audio stream)
Browser (headless, in cloud)
    ↓ (selects mechus_input as microphone)
PulseAudio Virtual Sink (mechus_input)
    ↓
Mechus Agent (listens)
    ↓ (processes, generates response)
Mechus Agent (speaks)
    ↓
PulseAudio Virtual Sink (mechus_output)
    ↓ (browser selects as speaker)
Browser
    ↓ (sends to meeting)
Meeting Platform
```

## Key Differences: Local vs Cloud

| Aspect | Local (Your Computer) | Cloud (Server) |
|--------|----------------------|----------------|
| **Audio Devices** | BlackHole (physical) | PulseAudio (virtual) |
| **Browser** | Visible or headless | Always headless + Xvfb |
| **Display** | Your screen | Virtual display (Xvfb) |
| **Setup** | One-time in browser | One-time in browser (via VNC/X11) |
| **Audio Flow** | Physical → Virtual | Virtual → Virtual |

## Setup Process for Cloud

### Initial Setup (One-Time)

1. **Deploy to cloud** (AWS/Azure/DigitalOcean)
2. **Set up virtual audio** (PulseAudio)
3. **Install Xvfb** (for headless display)
4. **First meeting join:**
   - Use VNC or X11 forwarding to see browser
   - Join test meeting
   - Set audio devices to virtual devices
   - Chrome saves settings
5. **After setup:** Everything automated

### Ongoing (After Setup)

1. **Dashboard calls API** → Meeting joins automatically
2. **Browser invisible** → Runs in background
3. **Audio automatic** → Uses saved virtual device settings
4. **Fully automated** → No manual steps

## Environment Variables for Cloud

```bash
# Audio devices (virtual)
MECHUS_INPUT_DEVICE=mechus_input
MECHUS_OUTPUT_DEVICE=mechus_output

# Browser
MECHUS_SHOW_BROWSER=false  # Always headless in cloud
DISPLAY=:99  # Xvfb virtual display

# API Server
MECHUS_API_HOST=0.0.0.0  # Listen on all interfaces
MECHUS_API_PORT=5000
```

## Troubleshooting Cloud Audio

**Audio not working:**
```bash
# Check virtual audio devices exist
pactl list sinks | grep mechus

# Check PulseAudio is running
pulseaudio --check

# Restart PulseAudio
pulseaudio --kill
pulseaudio --start
```

**Browser not working:**
```bash
# Check Xvfb is running
ps aux | grep Xvfb

# Check display
echo $DISPLAY  # Should be :99

# Test Chrome
xvfb-run -a google-chrome --version
```

## Alternative: Meeting Platform APIs

For completely programmatic control (no browser):

- **Zoom SDK:** Join via API, set audio programmatically
- **Google Meet API:** Limited, but possible
- **More complex:** Requires API credentials

See `ZOOM_SDK_SETUP.md` for details.

## Summary

**In the cloud:**
1. ✅ Virtual audio devices (PulseAudio) replace BlackHole
2. ✅ Browser automation works the same (with Xvfb)
3. ✅ Chrome profile saves audio settings
4. ✅ One-time setup (join test meeting, set audio)
5. ✅ After setup: Fully automated

The main difference is using **virtual audio devices** instead of physical ones, but the automation works the same way.
