# Per-participant audio and Zoom display names (speaker attribution)

By default, Mechus uses **mixed** meeting audio (everyone in one stream). That is best for reliably hearing **all** participants (including the wake word) but transcripts are often labeled `unknown` unless you run **diarization** on the mixed audio (anonymous `SPEAKER_00`, etc.).

To get **separate audio streams per participant** and map them to the **name each person joined Zoom with** (true “who said what” without pyannote clusters), use **Zoom stream attribution**.

---

## What to set

```env
# Enable one-way raw audio only; each frame is tagged with that participant’s display name from Zoom.
MECHUS_ZOOM_STREAM_ATTRIBUTION=1
```

**Turn off mixed-only diarization** for this mode (Mechus disables the diarizer automatically when stream attribution is on):

```env
MECHUS_DIARIZATION_ENABLED=0
```

If you leave `MECHUS_DIARIZATION_ENABLED=1`, the agent logs that diarization is disabled because each Zoom stream is already one speaker.

---

## How it works (implementation)

1. **`mechus/zoom_sdk_join.py`**  
   With `stream_attribution=True`, the **mixed** audio callback is **not** used (so you don’t double‑count audio). Only **`onOneWayAudioRawDataReceived`** runs, with a **Zoom `user_id` (node_id)** per chunk.

2. **Names**  
   `zoom_resolve_participant_label()` calls the Zoom Meeting SDK **participant controller** (`GetUserByUserID` / `GetUserName`) → **in-meeting display name** as shown in Zoom. If the SDK cannot resolve a name yet, the label is `user_<uid>` until the roster updates.

3. **`MechusAgent`**  
   Each distinct `speaker_id` gets its own **`TranscriptChunker`** so VAD/chunking is per stream. Chunks are transcribed with **`speaker_id` = that label**, then ingested into memory / context engine / meeting store as e.g. `"Alice: ..."`.

4. **Optional overrides**  
   **`POST /api/meeting/speakers`** with `{ "meeting_id": "...", "speakers": { "zoom:123": "Alice", "Bob": "Robert" } }` merges into `mechus_notes/speaker_maps/<safe_meeting_id>.json`. The Zoom worker reloads that file (and any legacy flat `speaker-map-<meeting_id>.json`) and applies **`set_label_override`** on the audio input so labels can be corrected without restarting.

---

## Tradeoffs

| Mode | Audio | Speaker labels |
|------|--------|----------------|
| **Default** (`MECHUS_ZOOM_STREAM_ATTRIBUTION=0`) | Mixed (everyone) | Often `unknown` or pyannote `SPEAKER_xx` if diarization is on |
| **Stream attribution** (`=1`) | One-way per participant | Zoom **display names** (or `user_<id>`) |

- **Wake word:** Still works; frames arrive in time order from different streams. If you ever see missed wake, compare with mixed mode on the same meeting.
- **SDK behavior:** Requires Zoom raw recording + one-way subscription; same as normal join, but mixed is not fed to the agent.

---

## Quick checklist

1. Linux + Zoom SDK join (`join_backend=zoom_sdk`).
2. Set `MECHUS_ZOOM_STREAM_ATTRIBUTION=1` and `MECHUS_DIARIZATION_ENABLED=0` (recommended).
3. Deploy / restart the worker.
4. Optional: call **`POST /api/meeting/speakers`** to fix `user_<id>` → display names if the roster was late.

See also: `docs/ZOOM_SDK_SETUP.md`.
