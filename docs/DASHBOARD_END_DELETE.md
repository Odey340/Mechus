# End meeting / Delete meeting → Mechus stop

When you click **End meeting** or **Delete meeting** on the dashboard, the Mechus API (this server) must stop the session so the bot leaves the meeting and the user can start another.

## What happens here (Mechus API)

- The dashboard backend calls **`POST /api/stop`** with the same `meeting_id` that was used for join/status.
- This API (in `mechus/api_server.py`) then:
  - Stops the agent and leaves the Zoom meeting (or closes the browser for Google Meet).
  - Removes the session from `_active_sessions`.
- After that, the user is no longer blocked by “User already has an active meeting”.

## What the dashboard must do

| Action            | Dashboard call                          | Then dashboard calls Mechus      |
|------------------|-----------------------------------------|---------------------------------|
| **End meeting**  | `POST /api/meetings/{id}/end`           | `POST {MECHUS_API_URL}/api/stop` with `meeting_id` |
| **Delete meeting** | `DELETE /api/meetings/{id}/delete`   | `POST {MECHUS_API_URL}/api/stop` with `meeting_id` |
| **Cancel (joining)** | `PATCH /api/meetings/{id}/update-state` with `status: "failed"` | `POST {MECHUS_API_URL}/api/stop` with `meeting_id` |

The `meeting_id` must be the **Supabase meeting UUID** (same as the one sent to `POST /api/join-meeting` when the meeting was started).

## Response body: `final_summary` for the dashboard

When the meeting had transcript/state, the stop response includes **`final_summary`** so the dashboard can persist and display it:

```json
{
  "success": true,
  "message": "Meeting stopped",
  "meeting_id": "uuid-here",
  "final_summary": {
    "meeting_id": "uuid-here",
    "generated_at": 1234567890.123,
    "executive_summary": "2–4 sentences for leadership: what was discussed, key outcomes, next steps.",
    "action_items": [
      { "owner": "Name", "task": "What to do", "deadline": "by Friday" }
    ],
    "decisions": [
      { "id": "decision_1", "value": "We decided to..." }
    ],
    "open_questions": [ "Topic still open", "..." ],
    "interactions": [
      { "user_query": "Can you recap the meeting?", "mechus_response": "We discussed...", "ts": 1234567890.1 }
    ],
    "source": "llm"
  }
}
```

- **`final_summary`** may be `null` if the meeting had no transcript, the worker exited before writing, or the LLM call failed (fallback state-only summary is still returned when possible).
- **`interactions`** is the list of Mechus Q&A turns: each item has `user_query` (what the user asked), `mechus_response` (what Mechus said), and optional `ts` (timestamp). Use this to show “What people asked Mechus and how Mechus replied” on the meeting recap screen.
- **`source`** is `"llm"` (full transcript + LLM), `"state_only"` (no API key / no LLM), or `"fallback"` (LLM failed, used state).
- The dashboard should store `executive_summary`, `action_items`, `decisions`, `open_questions`, and `interactions` (e.g. in Supabase for that meeting) and display them on the meeting-detail or recap screen.

## Summary

- **End** and **Delete** (and **Cancel**) in the dashboard should all trigger a call to this API’s **`POST /api/stop`** with that meeting’s id so the session is cleared here.
- On success, read **`final_summary`** from the response and save it for the meeting so users can see the executive summary and action items.
