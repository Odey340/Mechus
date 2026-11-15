# Auto-Join Meeting Solution

## Current Problem

When you paste a meeting link:
1. ✅ Link opens in browser
2. ❌ You still have to manually click "Join"
3. ❌ You still have to set audio devices manually

## Solutions

### Solution 1: Browser Automation (Recommended)

Use Selenium or Playwright to automatically click "Join" button in the browser.

**Install dependencies:**
```bash
pip install selenium playwright
playwright install chromium
```

**Update `koov/api_server.py` to auto-join:**

```python
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

def _open_and_join_meeting(link: str) -> None:
    """Open meeting link and automatically join."""
    # Start browser in headless mode (or visible for debugging)
    options = webdriver.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    # Remove headless if you want to see the browser
    # options.add_argument('--headless')
    
    driver = webdriver.Chrome(options=options)
    
    try:
        # Open meeting link
        driver.get(link)
        
        # Wait for page to load
        time.sleep(3)
        
        # Try to find and click "Join" button
        # Zoom
        try:
            join_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Join')]"))
            )
            join_button.click()
            time.sleep(2)
            
            # Click "Join with Computer Audio" if it appears
            try:
                audio_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Computer Audio') or contains(text(), 'Join Audio')]"))
                )
                audio_button.click()
            except:
                pass  # Button might not appear
                
        except:
            # Google Meet - different selectors
            try:
                join_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Join now')]"))
                )
                join_button.click()
            except:
                pass
        
        # Keep browser open (don't close it)
        # The meeting will stay open
        
    except Exception as e:
        log.error(f"Failed to auto-join meeting: {e}")
        # Fallback: just open the link normally
        _open_link(link)
```

### Solution 2: Meeting Platform APIs

Use Zoom/Google Meet APIs to join programmatically (more complex, requires API keys).

### Solution 3: Instructions + Manual Setup (Simplest)

Keep current behavior but add clear instructions for users.

## Recommended: Hybrid Approach

1. **Auto-open and attempt auto-join** (using browser automation)
2. **If auto-join fails, show instructions**
3. **Audio devices must be set up once** (can't be automated)

## Implementation

I'll update the API server to include browser automation for auto-joining.
