# Fix: Koov Not Auto-Joining Meetings

## Problem

Koov opens the meeting link but doesn't automatically click "Join" - you have to do it manually.

## Root Cause

**Selenium is not installed**, so browser automation doesn't work. The code falls back to just opening the link in Safari.

## Solution: Install Selenium and ChromeDriver

### Step 1: Install Selenium

```bash
source .venv/bin/activate
pip install selenium
```

### Step 2: Install ChromeDriver

ChromeDriver is needed for Selenium to control Chrome:

```bash
# Using Homebrew (recommended)
brew install chromedriver

# After installation, allow it to run (macOS security)
xattr -d com.apple.quarantine /usr/local/bin/chromedriver 2>/dev/null || \
xattr -d com.apple.quarantine $(brew --prefix)/bin/chromedriver 2>/dev/null
```

### Step 3: Verify Installation

```bash
# Check Selenium is installed
python3 -c "import selenium; print('Selenium OK')"

# Check ChromeDriver is installed
chromedriver --version

# Check Chrome is installed
ls "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

### Step 4: Restart API Server

After installing, restart your API server:

```bash
# Stop the server (Ctrl+C)
# Then restart
./START_SERVER.sh
```

## What Should Happen After Fix

1. ✅ Meeting link opens in **Chrome** (not Safari)
2. ✅ Chrome automatically clicks "Join" button
3. ✅ Koov joins the meeting automatically
4. ✅ No manual clicking needed

## Test It

1. Join a meeting from your dashboard
2. Watch the terminal logs - you should see:
   ```
   Using Chrome at: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
   Running browser in headless mode (invisible)
   Opened meeting link in browser
   Clicked Join button (Google Meet)  # or Zoom
   ```
3. Check if Koov joined automatically

## Troubleshooting

### "ChromeDriver version mismatch"

If ChromeDriver version doesn't match Chrome:

```bash
# Update ChromeDriver
brew upgrade chromedriver

# Or check your Chrome version and download matching ChromeDriver
# Chrome version: chrome://version/
# Download: https://chromedriver.chromium.org/downloads
```

### "Permission denied" for ChromeDriver

```bash
# Allow ChromeDriver to run
xattr -d com.apple.quarantine $(which chromedriver)
```

### Still opening Safari?

1. Check API server logs for errors
2. Verify ChromeDriver is in PATH: `which chromedriver`
3. Try setting Chrome path explicitly:
   ```bash
   export KOOV_CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
   ```

### Want to see the browser during setup?

For first-time audio device setup, show the browser:

```bash
export KOOV_SHOW_BROWSER=true
./START_SERVER.sh
```

This will show Chrome (not headless) so you can:
1. See the join process
2. Configure audio devices in Zoom/Meet settings
3. Chrome saves these settings automatically

After setup, remove the environment variable to run headless.

## After Everything Works

Once Selenium and ChromeDriver are installed:

1. ✅ Auto-join works automatically
2. ✅ Chrome profile saves audio settings
3. ✅ Future meetings use saved settings
4. ✅ No manual intervention needed
