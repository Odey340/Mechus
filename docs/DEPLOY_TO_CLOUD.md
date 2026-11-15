# Deploying Mechus to the Cloud

This guide shows how to deploy Mechus to a cloud server so it can run 24/7 and handle multiple meetings simultaneously.

## Architecture Overview

**Current (Local):**
```
Your Computer → Mechus API Server → Mechus Agent → Meeting
```

**Cloud (Deployed):**
```
Dashboard → Cloud Server → Mechus API Server → Multiple Mechus Agents → Multiple Meetings
```

## Deployment Options

### Option 1: VPS (Virtual Private Server) - Recommended

**Providers:**
- DigitalOcean ($6-12/month)
- Linode ($5-10/month)
- AWS EC2 (pay-as-you-go)
- Google Cloud Compute Engine
- Azure Virtual Machines

**Pros:**
- Full control
- Can install audio drivers
- Cost-effective
- Easy to scale

**Cons:**
- Need to manage server
- Audio setup can be complex

### Option 2: Container Platform (Docker + Kubernetes)

**Providers:**
- AWS ECS/Fargate
- Google Cloud Run
- Azure Container Instances
- DigitalOcean App Platform

**Pros:**
- Easy scaling
- Managed infrastructure
- Auto-scaling

**Cons:**
- Audio devices in containers is complex
- May need special configuration

### Option 3: Serverless (Not Recommended for Audio)

**Why not:** Audio processing requires persistent connections and audio devices, which don't work well with serverless.

## Step-by-Step: Deploy to VPS

### 1. Set Up VPS

**Choose a provider and create a server:**
- OS: Ubuntu 22.04 LTS (recommended)
- Size: 2-4 GB RAM, 2+ vCPUs (for multiple meetings)
- Location: Choose closest to your users

**SSH into your server:**
```bash
ssh root@your-server-ip
```

### 2. Install Dependencies

```bash
# Update system
apt update && apt upgrade -y

# Install Python and dependencies
apt install -y python3.11 python3.11-venv python3-pip git

# Install audio dependencies (for virtual audio devices)
apt install -y pulseaudio pulseaudio-utils alsa-utils

# Install system dependencies
apt install -y build-essential portaudio19-dev libasound2-dev
```

### 3. Set Up Virtual Audio (Critical for Cloud)

**For Linux servers, you'll need PulseAudio or JACK:**

```bash
# Install PulseAudio with virtual sink
apt install -y pulseaudio pulseaudio-module-null

# Create virtual audio device
pactl load-module module-null-sink sink_name=mechus_input sink_properties=device.description="Mechus Input"
pactl load-module module-null-sink sink_name=mechus_output sink_properties=device.description="Mechus Output"
```

**Update your `.env` file:**
```bash
MECHUS_INPUT_DEVICE=mechus_input
MECHUS_OUTPUT_DEVICE=mechus_output
```

### 4. Deploy Mechus Code

```bash
# Clone your repo (or upload files)
git clone https://github.com/yourusername/mechusdemo.git
cd mechusdemo

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Download models
python3 scripts/download_vosk_model.py --out models
```

### 5. Set Up Environment Variables

```bash
# Create .env file
nano .env
```

Add your configuration:
```bash
# Audio devices (virtual)
MECHUS_INPUT_DEVICE=mechus_input
MECHUS_OUTPUT_DEVICE=mechus_output

# API Server
MECHUS_API_HOST=0.0.0.0  # Listen on all interfaces
MECHUS_API_PORT=5000

# Your other config...
MECHUS_WAKE_PHRASE=computer
MECHUS_TTS_BACKEND=elevenlabs
MECHUS_ELEVEN_API_KEY=your_key
# etc.
```

### 6. Set Up Process Manager (PM2 or systemd)

**Option A: Using PM2 (Recommended)**

```bash
# Install PM2
npm install -g pm2

# Start API server with PM2
cd /path/to/mechusdemo
source .venv/bin/activate
export PYTHONPATH="$PWD"
pm2 start scripts/start_api_server.py --name mechus-api --interpreter python3

# Save PM2 configuration
pm2 save
pm2 startup  # Follow instructions to enable on boot
```

**Option B: Using systemd**

Create service file:
```bash
sudo nano /etc/systemd/system/mechus-api.service
```

```ini
[Unit]
Description=Mechus API Server
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/mechusdemo
Environment="PATH=/path/to/mechusdemo/.venv/bin"
Environment="PYTHONPATH=/path/to/mechusdemo"
ExecStart=/path/to/mechusdemo/.venv/bin/python3 scripts/start_api_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable mechus-api
sudo systemctl start mechus-api
sudo systemctl status mechus-api
```

### 7. Set Up Reverse Proxy (Nginx)

```bash
# Install Nginx
apt install -y nginx

# Create Nginx config
sudo nano /etc/nginx/sites-available/mechus-api
```

```nginx
server {
    listen 80;
    server_name your-domain.com;  # or your server IP

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Enable and restart:
```bash
sudo ln -s /etc/nginx/sites-available/mechus-api /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 8. Set Up SSL (Let's Encrypt)

```bash
# Install Certbot
apt install -y certbot python3-certbot-nginx

# Get SSL certificate
sudo certbot --nginx -d your-domain.com
```

### 9. Update Dashboard Configuration

**In your dashboard `.env.local`:**
```bash
# Change from localhost to your server
MECHUS_API_URL=https://your-domain.com
# or
MECHUS_API_URL=http://your-server-ip:5000
```

## Handling Multiple Meetings

### Current Limitation

The current API server only handles one meeting at a time. To support multiple meetings, you need to:

### Option A: Multiple API Servers (Simple)

Run multiple instances on different ports:
```bash
# Server 1
MECHUS_API_PORT=5000 pm2 start scripts/start_api_server.py --name mechus-api-1

# Server 2
MECHUS_API_PORT=5001 pm2 start scripts/start_api_server.py --name mechus-api-2
```

### Option B: Modify API Server for Multiple Sessions (Better)

Update `mechus/api_server.py` to support multiple concurrent sessions:

```python
# Change from single session to session dictionary
_active_sessions: dict[str, MechusSession] = {}  # keyed by meeting_id

@app.route("/api/join-meeting", methods=["POST"])
def join_meeting():
    # Get meeting_id from request
    meeting_id = request.json.get("meeting_id")
    
    # Create session for this meeting
    _active_sessions[meeting_id] = MechusSession(...)
    
    # ... rest of code
```

## Audio Challenges in Cloud

**Problem:** Cloud servers don't have physical audio devices.

**Solutions:**

1. **Virtual Audio Devices (PulseAudio/JACK)**
   - Works for processing
   - Meeting audio comes through network (Zoom/Meet API)

2. **Meeting Platform APIs**
   - Use Zoom API / Google Meet API to get audio streams
   - Process audio streams directly
   - More complex but more reliable

3. **Hybrid Approach**
   - Cloud server processes audio
   - Use meeting platform webhooks/APIs
   - Stream audio to/from cloud

## Security Considerations

1. **Firewall:**
```bash
# Only allow necessary ports
ufw allow 22/tcp   # SSH
ufw allow 80/tcp   # HTTP
ufw allow 443/tcp  # HTTPS
ufw enable
```

2. **API Authentication:**
   - Add API keys to Mechus API server
   - Use JWT tokens
   - Rate limiting

3. **Environment Variables:**
   - Never commit `.env` files
   - Use secrets management (AWS Secrets Manager, etc.)

## Monitoring

**Set up monitoring:**
```bash
# PM2 monitoring
pm2 monit

# Or use PM2 web interface
pm2 web
```

**Logs:**
```bash
# View logs
pm2 logs mechus-api

# Or systemd
journalctl -u mechus-api -f
```

## Cost Estimation

**VPS (DigitalOcean/Linode):**
- Small: $6-12/month (1-2 meetings)
- Medium: $24-48/month (5-10 meetings)
- Large: $48-96/month (10+ meetings)

**Additional costs:**
- Domain name: $10-15/year
- SSL: Free (Let's Encrypt)
- Bandwidth: Usually included

## Quick Start Checklist

- [ ] Set up VPS (Ubuntu 22.04)
- [ ] Install Python and dependencies
- [ ] Set up virtual audio devices
- [ ] Deploy Mechus code
- [ ] Configure environment variables
- [ ] Set up process manager (PM2 or systemd)
- [ ] Configure reverse proxy (Nginx)
- [ ] Set up SSL certificate
- [ ] Update dashboard to point to cloud server
- [ ] Test with a meeting
- [ ] Set up monitoring and logging

## Next Steps

1. **Start with a small VPS** to test
2. **Test audio processing** - make sure virtual audio works
3. **Scale up** as needed
4. **Consider meeting platform APIs** for better audio handling

## Alternative: Managed Service

If you want to avoid server management, consider:
- **AWS ECS/Fargate** with custom audio setup
- **Google Cloud Run** (may need special config)
- **Managed Kubernetes** (GKE, EKS)

But these are more complex and expensive.
