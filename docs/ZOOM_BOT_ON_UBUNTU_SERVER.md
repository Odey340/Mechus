# Run the Zoom SDK bot on an Ubuntu server

Use this to have the **bot** join Zoom meetings from a Linux server (e.g. EC2 at `3.16.207.181`). You trigger join from your Mac; the bot runs on the server.

---

## 1. Get the code and `.env` on the server

**Option A: Clone on the server (if the repo is on GitHub)**

```bash
ssh -i ~/Downloads/mechus-key.pem ubuntu@3.16.207.181
cd ~
git clone https://github.com/YOUR_ORG/mechusdemo.git   # or your repo URL
cd mechusdemo
```

**Option B: Copy from your Mac with rsync**

From your **Mac** (in a new terminal, from the repo root):

```bash
rsync -avz -e "ssh -i ~/Downloads/mechus-key.pem" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  ./ ubuntu@3.16.207.181:~/mechusdemo/
```

Then on the **server**:

```bash
cd ~/mechusdemo
```

---

## 2. Create or edit `.env` on the server

On the **server**:

```bash
cd ~/mechusdemo
cp .env.example .env
nano .env   # or vim
```

Set at least:

- **Zoom (required for bot join):**
  - `ZOOM_APP_CLIENT_ID=` your Meeting SDK app Client ID  
  - `ZOOM_APP_CLIENT_SECRET=` your Meeting SDK app Client Secret  

- **TTS (so the bot can speak):**
  - `MECHUS_TTS_BACKEND=elevenlabs`
  - `MECHUS_ELEVEN_API_KEY=` your ElevenLabs API key  
  - `MECHUS_ELEVEN_VOICE_ID=` your ElevenLabs voice ID  

- **LLM (optional but recommended):**
  - `MECHUS_LLM_BACKEND=openai`
  - `MECHUS_OPENAI_API_KEY=` your OpenAI API key  

Save and exit.

---

## 3. Stop the current stack (if it’s running)

On the **server**:

```bash
cd ~/mechusdemo
docker compose down
# Or if you used the zoom script before:
# docker compose -f docker-compose.yml -f docker-compose.zoom.yml down
```

---

## 4. Build and run the Zoom SDK stack

On the **server** (Linux has `docker compose`; no need for the Mac script):

```bash
cd ~/mechusdemo
docker compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

- First run can take several minutes (Zoom SDK image is larger).  
- The API listens on **port 5001** on the host (from the base compose).

Check:

```bash
docker compose -f docker-compose.yml -f docker-compose.zoom.yml ps
curl -s http://127.0.0.1:5001/health
```

You should see `{"status":"ok",...}`.

---

## 5. Open port 5001 on the server (AWS Security Group)

If you call the API from your **Mac**, the server must allow inbound TCP **5001**.

- **AWS EC2:** EC2 → Security Groups → select the group for this instance → Inbound rules → Edit → Add rule: Type **Custom TCP**, Port **5001**, Source **My IP** (or **0.0.0.0/0** for testing). Save.

---

## 6. Trigger the bot to join from your Mac

Replace `3.16.207.181` with your server’s public IP (or hostname). Use a **real Zoom meeting link** (with meeting number and `?pwd=...` if there’s a passcode).

```bash
curl -X POST http://3.16.207.181:5001/api/join-meeting \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_link": "https://zoom.us/j/YOUR_MEETING_NUMBER?pwd=YOUR_PASSWORD",
    "join_backend": "zoom_sdk"
  }'
```

- If the API is on a different port, use that port instead of `5001`.  
- You can add `"user_id": "user-1"` or `"meeting_id": "my-meeting-1"` if your dashboard uses them.

**In the meeting:** Join the same Zoom meeting as a normal participant (from your Mac or phone). When the bot joins, the **host** must click **Allow** when Zoom asks to allow recording so the bot gets audio.

---

## 7. Check logs on the server

```bash
cd ~/mechusdemo
docker compose -f docker-compose.yml -f docker-compose.zoom.yml logs -f mechus
```

Look for:

- `Zoom audio: first frame received; Mechus can hear the meeting` → audio is working.  
- `CanStartRawRecording returned ... (host must allow recording)` → host has not allowed yet.

---

## Troubleshooting

- **"Zoom SDK is not available"** → You’re not using the Zoom image. Use the exact command in step 4 (both compose files).  
- **Bot joins but no audio** → Host must allow recording when Zoom prompts; someone must be speaking; see [MECHUS_HEAR_ME.md](MECHUS_HEAR_ME.md).  
- **Connection refused from Mac** → Security group or firewall must allow TCP 5001 from your IP.  
- **Permission denied** on `docker-compose-up.sh` → On Linux you don’t need that script; use `docker compose -f docker-compose.yml -f docker-compose.zoom.yml` as above.
