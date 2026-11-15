# Fix Chrome/Safari Issue

## Problem

When joining a meeting, Safari opens instead of Chrome, asking for microphone permissions.

## Why This Happens

The API server tries to use Selenium with Chrome for browser automation, but if Chrome/ChromeDriver isn't properly configured, it falls back to opening the default browser (Safari on macOS).

## Solution: Install Chrome and ChromeDriver

### Step 1: Install Google Chrome

If you don't have Chrome installed:

1. Download Chrome: https://www.google.com/chrome/
2. Install it in `/Applications/Google Chrome.app`

### Step 2: Install ChromeDriver

ChromeDriver is needed for Selenium to control Chrome:

```bash
# Using Homebrew (recommended)
brew install chromedriver

# Or download manually from:
# https://chromedriver.chromium.org/downloads
```

**Important:** After installing ChromeDriver, you may need to allow it to run:

```bash
# Remove quarantine attribute (macOS security)
xattr -d com.apple.quarantine /usr/local/bin/chromedriver 2>/dev/null || \
xattr -d com.apple.quarantine $(brew --prefix)/bin/chromedriver 2>/dev/null
```

### Step 3: Verify Installation

```bash
# Check ChromeDriver is installed
chromedriver --version

# Check Chrome is installed
ls "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

### Step 4: Restart API Server

After installing Chrome and ChromeDriver:

```bash
# Stop the API server (Ctrl+C)
# Then restart it
source .venv/bin/activate
export PYTHONPATH="$PWD"
python3 scripts/start_api_server.py
```

### Step 5: Test Again

Try joining a meeting from your dashboard. It should now:
- ✅ Open Chrome (not Safari)
- ✅ Use the Mechus Chrome profile (`~/.mechus-chrome-profile`)
- ✅ Remember audio device settings after first setup

## Alternative: Use Visible Browser for Setup

If you want to see the browser during setup (to configure audio devices):

```bash
# Set environment variable before starting server
export MECHUS_SHOW_BROWSER=true
python3 scripts/start_api_server.py
```

This will show Chrome (not headless) so you can:
1. Join the meeting
2. Configure audio devices in Zoom/Meet settings
3. Chrome saves these settings to the profile
4. Future meetings use saved settings automatically

## Troubleshooting

### "ChromeDriver version mismatch"

If you get a version mismatch error:

```bash
# Update ChromeDriver to match your Chrome version
brew upgrade chromedriver

# Or check Chrome version and download matching ChromeDriver
# Chrome version: chrome://version/
# ChromeDriver: https://chromedriver.chromium.org/downloads
```

### "Permission denied" for ChromeDriver

```bash
# Allow ChromeDriver to run
sudo spctl --master-disable  # Temporarily disable Gatekeeper
# Or use xattr command above
```

### Still opening Safari?

1. Check API server logs for errors
2. Verify ChromeDriver is in PATH: `which chromedriver`
3. Try setting Chrome path explicitly in `.env`:
   ```
   MECHUS_CHROME_BINARY=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
   ```

### Want to use Safari instead?

If you prefer Safari, you'd need to:
1. Use Safari's WebDriver (safaridriver)
2. Configure it differently
3. But Chrome is recommended for better automation support

## After Setup

Once Chrome is working:
1. Join a test meeting
2. Configure audio devices in Zoom/Meet:
   - Microphone: BlackHole 2ch (or your input device)
   - Speaker: Your Multi-Output device
3. Chrome saves these settings
4. Future meetings use saved settings automatically
5. You can set `MECHUS_SHOW_BROWSER=false` to run headless
