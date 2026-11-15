# Cloud Deployment Readiness Checklist

Use this checklist to determine if you're ready to deploy Mechus to the cloud.

## ✅ Code & Features (All Complete!)

- [x] **API Server** - Flask server with all endpoints
- [x] **Multiple Meetings** - Support for concurrent meetings
- [x] **User Limits** - One meeting per user enforcement
- [x] **Auto-Join** - Browser automation with Selenium
- [x] **Chrome Profile** - Persistent audio device settings
- [x] **Status Tracking** - Real-time status polling
- [x] **Transcripts** - Beautiful formatted transcripts
- [x] **Stop Meeting** - Graceful session termination
- [x] **Headless Browser** - Runs invisibly in background

## 🔍 Pre-Deployment Testing (Do These First!)

### 1. Test with Docker (production path)
```bash
./docker-compose-up.sh up -d --build
# Or with Zoom SDK: ./docker-compose-up.sh -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

**Check:**
- [ ] Container starts; health works: `curl http://localhost:5001/health`
- [ ] Can join a test meeting (dashboard or `POST /api/join-meeting`)
- [ ] Status endpoint returns correct data: `GET /api/status?meeting_id=...`
- [ ] Can stop a meeting: `POST /api/stop`
- [ ] Transcript endpoint works

For dev-only local server (no Docker), see [DEV_LOCAL_SETUP.md](DEV_LOCAL_SETUP.md).

### 2. Test with Your Dashboard
- [ ] Dashboard can call `/api/join-meeting` with `user_id`
- [ ] Dashboard polls status correctly
- [ ] Dashboard receives transcripts
- [ ] Dashboard can stop meetings
- [ ] Error handling works (e.g., user already has meeting)

### 3. Verify Environment Variables
Check your `.env` file has all required values:

**Required:**
- [ ] `MECHUS_INPUT_DEVICE` - Your local device (will change in cloud)
- [ ] `MECHUS_OUTPUT_DEVICE` - Your local device (will change in cloud)
- [ ] `MECHUS_WAKE_PHRASE` - e.g., "computer"
- [ ] `MECHUS_VOSK_MODEL_PATH` - Path to Vosk model
- [ ] `MECHUS_WHISPER_MODEL` - e.g., "base.en"

**Optional but Recommended:**
- [ ] `MECHUS_TTS_BACKEND=elevenlabs`
- [ ] `MECHUS_ELEVEN_API_KEY` - Your ElevenLabs API key
- [ ] `MECHUS_ELEVEN_VOICE_ID` - Your voice ID
- [ ] `MECHUS_LLM_BACKEND=ollama` (if using Ollama)
- [ ] `MECHUS_OLLAMA_HOST` - Where Ollama runs (cloud or local)

**For Cloud:**
- [ ] `MECHUS_API_HOST=0.0.0.0` - Listen on all interfaces
- [ ] `MECHUS_API_PORT=5000` - Port for API server

## ☁️ Cloud Provider Setup

### Choose Your Provider
- [ ] **DigitalOcean** (Recommended - $6/month) - Best balance
- [ ] **AWS EC2** (Free tier available) - More complex
- [ ] **Azure VM** (Free tier available) - More complex
- [ ] **Linode** ($5/month) - Budget option

### Account & Access
- [ ] Created cloud provider account
- [ ] Have SSH key pair ready
- [ ] Know how to SSH into server
- [ ] Have payment method set up (if not free tier)

## 🎯 Cloud-Specific Requirements

### 1. Virtual Audio Setup
**You'll need to:**
- [ ] Install PulseAudio on cloud server
- [ ] Create virtual audio devices (`mechus_input`, `mechus_output`)
- [ ] Update `.env` with virtual device names
- [ ] Test audio flow works

**Documentation:** See `CLOUD_AUDIO_SETUP.md`

### 2. Chrome Profile Setup (One-Time)
**You'll need to:**
- [ ] Set up VNC or X11 forwarding (or use `MECHUS_SHOW_BROWSER=true`)
- [ ] Join a test meeting manually
- [ ] Configure audio devices in Zoom/Meet settings
- [ ] Chrome saves settings to `~/.mechus-chrome-profile`
- [ ] Future meetings use saved settings automatically

**Documentation:** See `SETUP_CHROME_PROFILE.md`

### 3. Process Management
**Choose one:**
- [ ] **PM2** (Recommended) - Easy to use
- [ ] **systemd** - More traditional Linux way

**Documentation:** See `DEPLOY_TO_CLOUD.md`

### 4. Security
- [ ] Firewall configured (allow port 5000)
- [ ] API server behind reverse proxy (Nginx) with HTTPS
- [ ] Environment variables secured (not in git)
- [ ] SSH keys only (disable password auth)

## 📋 Deployment Steps (When Ready)

1. **Create Server**
   - [ ] Choose provider and create VM
   - [ ] SSH into server
   - [ ] Install dependencies (Python, git, etc.)

2. **Set Up Audio**
   - [ ] Install PulseAudio
   - [ ] Create virtual audio devices
   - [ ] Test audio devices work

3. **Deploy Code**
   - [ ] Clone your repo
   - [ ] Create virtual environment
   - [ ] Install dependencies
   - [ ] Download models

4. **Configure Environment**
   - [ ] Create `.env` file with cloud settings
   - [ ] Set virtual audio device names
   - [ ] Add API keys
   - [ ] Set `MECHUS_API_HOST=0.0.0.0`

5. **One-Time Chrome Setup**
   - [ ] Set up VNC/X11 or use visible browser
   - [ ] Join test meeting
   - [ ] Configure audio devices
   - [ ] Verify Chrome profile saves settings

6. **Start Service**
   - [ ] Start API server with PM2 or systemd
   - [ ] Verify it's running
   - [ ] Test health endpoint
   - [ ] Test joining a meeting

7. **Connect Dashboard**
   - [ ] Update dashboard `MECHUS_API_URL` to cloud server
   - [ ] Test end-to-end flow
   - [ ] Verify multiple users can have meetings

## ⚠️ Common Gotchas

- [ ] **Audio devices** - Must use virtual devices in cloud, not BlackHole
- [ ] **Chrome profile** - One-time manual setup required
- [ ] **Ollama** - If using Ollama, need to run it on cloud or different server
- [ ] **Firewall** - Must open port 5000 (or your chosen port)
- [ ] **HTTPS** - Use Nginx reverse proxy for production
- [ ] **Environment variables** - Don't commit `.env` to git

## 🎯 Readiness Score

**Count your checkmarks:**

- **20+ checkmarks** = ✅ Ready to deploy!
- **15-19 checkmarks** = ⚠️ Almost ready, finish missing items
- **<15 checkmarks** = 🔴 Not ready yet, complete more items

## 🚀 Quick Start Path

**If you want to deploy ASAP:**

1. ✅ Test API server locally (30 min)
2. ✅ Choose DigitalOcean ($6/month) (5 min)
3. ✅ Create server and SSH in (10 min)
4. ✅ Follow `DEPLOY_TO_CLOUD.md` step-by-step (1-2 hours)
5. ✅ One-time Chrome profile setup (15 min)
6. ✅ Test with dashboard (30 min)

**Total time: ~3-4 hours for first deployment**

## 📚 Documentation Reference

- `DEPLOY_TO_CLOUD.md` - General deployment guide
- `DEPLOY_AWS.md` - AWS-specific steps
- `DEPLOY_AZURE.md` - Azure-specific steps
- `CLOUD_AUDIO_SETUP.md` - Virtual audio setup
- `SETUP_CHROME_PROFILE.md` - Chrome profile setup
- `BEST_OPTION_FOR_MECHUS.md` - Provider recommendations
- `CHEAPEST_DEPLOYMENT.md` - Cost comparison

## 💡 Pro Tips

1. **Start small** - Use free tier or $6/month option first
2. **Test locally first** - Make sure everything works before deploying
3. **One meeting at a time** - Test with one meeting before scaling
4. **Monitor logs** - Use `pm2 logs mechus-api` or `journalctl` to debug
5. **Backup `.env`** - Keep a secure copy of your environment variables
