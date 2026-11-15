# Run Koov and test voice output

Quick path: run the bot on your VM, join a meeting, then trigger a test phrase so the bot speaks in the meeting.

---

## 1. Run Koov on the VM

**Start the Zoom SDK stack** (on the Ubuntu server):

```bash
cd ~/koovdemo
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml down
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

API is on **port 5001**. From your Mac, ensure the VM allows inbound **5001** (e.g. AWS Security Group).

---

## 2. Configure voice output (TTS)

On the **server**, edit `~/koovdemo/.env` and set:

```bash
KOOV_TTS_BACKEND=elevenlabs
KOOV_ELEVEN_API_KEY=your_elevenlabs_api_key
KOOV_ELEVEN_VOICE_ID=your_voice_id
```

- Get API key and voice ID from [ElevenLabs](https://elevenlabs.io/). Voice ID is in Voice Settings → Voice ID.
- Restart the stack after changing `.env`:  
  `docker-compose -f docker-compose.yml -f docker-compose.zoom.yml down && docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d`

---

## 3. Join a meeting

**From your Mac** (replace with your server IP and a real Zoom link):

```bash
curl -X POST http://YOUR_SERVER_IP:5001/api/join-meeting \
  -H "Content-Type: application/json" \
  -d '{"meeting_link":"https://zoom.us/j/MEETING_NUMBER?pwd=PASSWORD","join_backend":"zoom_sdk"}'
```

Use the `meeting_id` from the response (often the Zoom meeting number as string). Join the same meeting yourself; when the bot joins, the **host** must click **Allow** when Zoom asks to allow recording.

---

## 4. Test voice output

After the bot is in the meeting and status is active, play a test phrase **into the meeting**:

```bash
curl -X POST http://YOUR_SERVER_IP:5001/api/test-voice \
  -H "Content-Type: application/json" \
  -d '{"meeting_id":"MEETING_ID_FROM_JOIN","text":"Hello, this is Koov. Voice test."}'
```

- **Success:** Response `"success": true`; participants should hear the phrase.
- **TTS no audio:** 503 with message about `KOOV_ELEVEN_API_KEY` and `KOOV_ELEVEN_VOICE_ID` → fix `.env` and restart.
- **Virtual mic not ready:** 503 "Zoom virtual mic not ready yet" → wait 10–20 seconds after join and try again, or check `GET /api/status?meeting_id=...` for `virtual_mic_ready: true`.

---

## 5. Check status

```bash
curl "http://YOUR_SERVER_IP:5001/api/status?meeting_id=MEETING_ID"
```

- `virtual_mic_ready: true` → bot can send TTS into the meeting.
- `zoom_frames_received` > 0 → bot is receiving meeting audio.

---

## Summary

| Step | Action |
|------|--------|
| 1 | On VM: run Zoom stack (`docker-compose ... up -d`). |
| 2 | On VM: set `KOOV_TTS_BACKEND=elevenlabs`, `KOOV_ELEVEN_API_KEY`, `KOOV_ELEVEN_VOICE_ID` in `.env`; restart stack. |
| 3 | From Mac: `POST /api/join-meeting` with Zoom link and `join_backend: "zoom_sdk"`. |
| 4 | Host allows recording; wait until bot is in meeting. |
| 5 | From Mac: `POST /api/test-voice` with `meeting_id` and optional `text`. |
| 6 | Confirm you hear the phrase in the meeting; use `/api/status` if something fails. |

See [ZOOM_BOT_ON_UBUNTU_SERVER.md](ZOOM_BOT_ON_UBUNTU_SERVER.md) for full server setup and [KOOV_HEAR_ME.md](KOOV_HEAR_ME.md) for audio troubleshooting.
