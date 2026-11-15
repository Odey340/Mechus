# Deploying Mechus to AWS

This guide walks you through deploying Mechus to AWS step-by-step.

## Architecture Options

### Option 1: EC2 (Recommended for Audio)
- Full control, can install audio drivers
- Best for audio processing
- Pay for what you use

### Option 2: ECS/Fargate (Container-based)
- Managed containers
- Auto-scaling
- More complex audio setup

### Option 3: Lambda (Not Recommended)
- Doesn't work for persistent audio connections

## Step-by-Step: AWS EC2

### 1. Create EC2 Instance

1. **Go to AWS Console** → EC2 → Launch Instance

2. **Choose AMI:**
   - Ubuntu Server 22.04 LTS (recommended)
   - Or Amazon Linux 2023

3. **Instance Type:**
   - `t3.medium` (2 vCPU, 4 GB RAM) - for 2-3 meetings
   - `t3.large` (2 vCPU, 8 GB RAM) - for 5-10 meetings
   - `t3.xlarge` (4 vCPU, 16 GB RAM) - for 10+ meetings

4. **Key Pair:**
   - Create new key pair or use existing
   - Download the `.pem` file (you'll need it to SSH)

5. **Network Settings:**
   - Allow SSH (port 22) from your IP
   - Allow HTTP (port 80) from anywhere
   - Allow HTTPS (port 443) from anywhere
   - Allow custom TCP (port 5000) from anywhere (or just your dashboard IP)

6. **Storage:**
   - 20-30 GB should be enough
   - GP3 SSD recommended

7. **Launch Instance**

### 2. Connect to EC2 Instance

```bash
# Make key file executable
chmod 400 your-key.pem

# SSH into instance
ssh -i your-key.pem ubuntu@your-ec2-ip-address
```

### 3. Install Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip

# Install system dependencies
sudo apt install -y git build-essential
sudo apt install -y portaudio19-dev libasound2-dev
sudo apt install -y pulseaudio pulseaudio-utils alsa-utils

# Install Node.js (for PM2)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Install Chrome/Chromium and ChromeDriver (for browser automation)
sudo apt install -y chromium-browser chromium-chromedriver
# Or for Google Chrome:
# wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
# sudo apt install -y ./google-chrome-stable_current_amd64.deb

### 4. Set Up Virtual Audio and Display

```bash
# Install PulseAudio and Xvfb (for headless browser)
sudo apt install -y pulseaudio pulseaudio-utils xvfb

# Start PulseAudio
pulseaudio --start --system=false

# Create virtual audio devices
pactl load-module module-null-sink sink_name=mechus_input sink_properties=device.description="Mechus Input"
pactl load-module module-null-sink sink_name=mechus_output sink_properties=device.description="Mechus Output"

# Make it persistent
echo 'load-module module-null-sink sink_name=mechus_input sink_properties=device.description="Mechus Input"' | sudo tee -a /etc/pulse/default.pa
echo 'load-module module-null-sink sink_name=mechus_output sink_properties=device.description="Mechus Output"' | sudo tee -a /etc/pulse/default.pa

# Set up Xvfb for headless browser (virtual display)
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 > /dev/null 2>&1 &

# Verify virtual audio devices
pactl list sinks | grep -A 5 mechus
```

### 5. Deploy Mechus Code

**Option A: Clone from Git**
```bash
# Clone your repository
git clone https://github.com/yourusername/mechusdemo.git
cd mechusdemo
```

**Option B: Upload Files**
```bash
# On your local machine, use SCP
scp -i your-key.pem -r /path/to/mechusdemo ubuntu@your-ec2-ip:/home/ubuntu/
```

**Set up Python environment:**
```bash
cd mechusdemo
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Download Vosk model
python3 scripts/download_vosk_model.py --out models
```

### 6. Configure Environment

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
MECHUS_API_HOST=0.0.0.0
MECHUS_API_PORT=5000

# Wake word
MECHUS_WAKE_PHRASE=computer
MECHUS_WAKE_BACKEND=vosk
MECHUS_VOSK_MODEL_PATH=./models/vosk-model-small-en-us-0.15

# STT
MECHUS_STT_BACKEND=faster-whisper
MECHUS_WHISPER_MODEL=base.en

# TTS
MECHUS_TTS_BACKEND=elevenlabs
MECHUS_ELEVEN_API_KEY=your_key_here
MECHUS_ELEVEN_VOICE_ID=your_voice_id

# LLM
MECHUS_LLM_BACKEND=ollama
MECHUS_OLLAMA_HOST=http://127.0.0.1:11434
MECHUS_OLLAMA_MODEL=your_model

# Memory
MECHUS_MEMORY_MAX_CHARS=20000
MECHUS_MEMORY_RECENT_CHARS=4000
```

### 7. Set Up Process Manager (PM2)

```bash
# Install PM2 globally
sudo npm install -g pm2

# Start Mechus API server
cd /home/ubuntu/mechusdemo
source .venv/bin/activate
export PYTHONPATH="$PWD"
pm2 start scripts/start_api_server.py --name mechus-api --interpreter python3

# Save PM2 configuration
pm2 save

# Set up PM2 to start on boot
pm2 startup
# Follow the instructions it prints (usually involves running a sudo command)
```

### 8. Set Up Nginx Reverse Proxy

```bash
# Install Nginx
sudo apt install -y nginx

# Create Nginx configuration
sudo nano /etc/nginx/sites-available/mechus-api
```

Add this configuration:
```nginx
server {
    listen 80;
    server_name your-domain.com;  # or use EC2 public IP

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Increase timeouts for long-running connections
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
```

Enable and test:
```bash
sudo ln -s /etc/nginx/sites-available/mechus-api /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default  # Remove default if exists
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx
```

### 9. Set Up SSL with Let's Encrypt

```bash
# Install Certbot
sudo apt install -y certbot python3-certbot-nginx

# Get SSL certificate (replace with your domain)
sudo certbot --nginx -d your-domain.com

# Auto-renewal is set up automatically
```

**Note:** If you don't have a domain, you can use the EC2 public IP, but SSL won't work (use HTTP only).

### 10. Configure Security Group

1. Go to EC2 → Security Groups
2. Find your instance's security group
3. Edit inbound rules:
   - SSH (22) - from your IP only
   - HTTP (80) - from anywhere (0.0.0.0/0)
   - HTTPS (443) - from anywhere (0.0.0.0/0)
   - Custom TCP (5000) - from your dashboard IP only (optional, if not using Nginx)

### 11. Get Your API URL

**With domain:**
```
https://your-domain.com
```

**Without domain (HTTP only):**
```
http://your-ec2-public-ip
```

### 12. Update Dashboard

In your dashboard `.env.local`:
```bash
MECHUS_API_URL=https://your-domain.com
# or
MECHUS_API_URL=http://your-ec2-public-ip:5000
```

## AWS ECS/Fargate (Alternative)

If you prefer containers:

### 1. Create Dockerfile

```dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    portaudio19-dev \
    libasound2-dev \
    pulseaudio \
    pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Set up virtual audio
RUN pulseaudio --start && \
    pactl load-module module-null-sink sink_name=mechus_input && \
    pactl load-module module-null-sink sink_name=mechus_output

# Expose port
EXPOSE 5000

# Run
CMD ["python3", "scripts/start_api_server.py"]
```

### 2. Build and Push to ECR

```bash
# Create ECR repository
aws ecr create-repository --repository-name mechus-api

# Get login token
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin your-account-id.dkr.ecr.us-east-1.amazonaws.com

# Build and push
docker build -t mechus-api .
docker tag mechus-api:latest your-account-id.dkr.ecr.us-east-1.amazonaws.com/mechus-api:latest
docker push your-account-id.dkr.ecr.us-east-1.amazonaws.com/mechus-api:latest
```

### 3. Create ECS Task Definition

Use AWS Console or CLI to create task definition with:
- Image: Your ECR image
- Port: 5000
- Environment variables: From your .env file
- CPU/Memory: 2 vCPU, 4 GB RAM minimum

### 4. Create ECS Service

- Launch type: Fargate
- VPC: Default or custom
- Subnets: Public subnets
- Security group: Allow port 5000
- Load balancer: Application Load Balancer (optional)

## Cost Estimation

**EC2:**
- t3.medium: ~$30/month (2-3 meetings)
- t3.large: ~$60/month (5-10 meetings)
- t3.xlarge: ~$120/month (10+ meetings)

**ECS Fargate:**
- Similar pricing to EC2
- Pay per task running

**Additional:**
- Data transfer: ~$0.09/GB
- Storage: ~$0.10/GB-month

## Monitoring

**CloudWatch:**
- Monitor CPU, memory, network
- Set up alarms
- View logs

**PM2 Monitoring:**
```bash
pm2 monit
pm2 logs mechus-api
```

## Troubleshooting

**Can't connect:**
- Check security group rules
- Verify instance is running
- Check Nginx status: `sudo systemctl status nginx`

**Audio not working:**
- Check PulseAudio: `pactl list sinks`
- Verify virtual sinks exist
- Check .env audio device names

**PM2 not starting:**
- Check logs: `pm2 logs mechus-api`
- Verify Python path
- Check environment variables

## Next Steps

1. Test with one meeting
2. Monitor resource usage
3. Scale up instance type if needed
4. Set up CloudWatch alarms
5. Consider multiple instances for high availability
