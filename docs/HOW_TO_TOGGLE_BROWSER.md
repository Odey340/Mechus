# How to Toggle Browser Visibility

## Default: Headless (Invisible)

By default, Koov runs Chrome in **headless mode** (invisible). You don't need to do anything - it's already set this way.

## Show Browser (For Setup)

To see the browser window (useful for first-time setup):

```bash
export KOOV_SHOW_BROWSER=true
./START_SERVER.sh
```

## Hide Browser (Headless)

To run in headless mode (invisible):

```bash
# Option 1: Unset the variable
unset KOOV_SHOW_BROWSER
./START_SERVER.sh

# Option 2: Explicitly set to false
export KOOV_SHOW_BROWSER=false
./START_SERVER.sh

# Option 3: Just don't set it (defaults to false)
./START_SERVER.sh
```

## Current Status

To check if browser is visible:

```bash
echo $KOOV_SHOW_BROWSER
```

- Empty or `false` = Headless (invisible) ✅
- `true` = Visible browser

## When to Use Each Mode

**Headless (invisible) - Default:**
- ✅ Normal operation
- ✅ After initial setup is complete
- ✅ Production/cloud deployment
- ✅ Multiple meetings running

**Visible browser:**
- 🔧 First-time setup
- 🔧 Debugging auto-join issues
- 🔧 Configuring audio devices
- 🔧 Testing new features

## Quick Toggle

```bash
# Show browser
export KOOV_SHOW_BROWSER=true && ./START_SERVER.sh

# Hide browser (default)
unset KOOV_SHOW_BROWSER && ./START_SERVER.sh
```
