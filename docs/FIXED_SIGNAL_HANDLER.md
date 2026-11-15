# Fixed: Signal Handler Error

## The Problem

When the Mechus API server tried to start the agent in a background thread, it failed with:
```
ValueError: signal only works in main thread of the main interpreter
```

This happened because `agent.run_forever()` tries to install signal handlers, but signal handlers can only be set in Python's main thread.

## The Fix

I've updated `mechus/api_server.py` to skip signal handler installation when running in a background thread. The agent will still work normally - it just won't respond to Ctrl+C signals (which is fine since it's running in the background anyway).

## Next Steps

1. **Stop the current API server** (Ctrl+C in the terminal where it's running)

2. **Restart it:**
   ```bash
   ./START_SERVER.sh
   # or
   source .venv/bin/activate
   export PYTHONPATH="$PWD"
   python3 scripts/start_api_server.py
   ```

3. **Test again** - The meeting should open and Mechus should start without errors!

The meeting link was successfully opened (I can see "Opening meeting link: https://meet.google.com/cdk-rmbz-hen" in your logs), so the integration is working - it just needed this fix.
