# Deploying Koov to Azure

This guide walks you through deploying Koov to Azure step-by-step.

## Architecture Options

### Option 1: Azure Virtual Machine (Recommended)
- Full control, can install audio drivers
- Best for audio processing
- Pay for what you use

### Option 2: Azure Container Instances
- Managed containers
- Simpler than VMs
- Audio setup more complex

### Option 3: Azure App Service (Not Recommended)
- Doesn't support persistent audio connections well

## Step-by-Step: Azure VM

### 1. Create Azure VM

1. **Go to Azure Portal** → Create a resource → Virtual Machine

2. **Basics:**
   - **Subscription:** Your subscription
   - **Resource Group:** Create new or use existing
   - **VM name:** `koov-api-server`
   - **Region:** Choose closest to your users
   - **Image:** Ubuntu Server 22.04 LTS
   - **Size:** 
     - Standard_B2s (2 vCPU, 4 GB RAM) - for 2-3 meetings
     - Standard_B2ms (2 vCPU, 8 GB RAM) - for 5-10 meetings
     - Standard_B4ms (4 vCPU, 16 GB RAM) - for 10+ meetings
   - **Authentication:** SSH public key (recommended) or password
   - **SSH public key:** Paste your public key

3. **Disks:**
   - OS disk: Premium SSD, 30 GB

4. **Networking:**
   - Virtual network: Create new or use existing
   - Subnet: Default
   - Public IP: Yes
   - NIC security group: Basic
   - Allow selected ports: SSH (22), HTTP (80), HTTPS (443), Custom (5000)

5. **Review + Create** → Create

### 2. Connect to Azure VM

```bash
# Get public IP from Azure Portal
ssh azureuser@your-vm-public-ip
```

Or use Azure Cloud Shell:
```bash
az vm show -d -g your-resource-group -n koov-api-server --query publicIps -o tsv
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

# Install Xvfb (for headless display)
sudo apt install -y xvfb

### 4. Set Up Virtual Audio and Display

```bash
# Install PulseAudio and Xvfb
sudo apt install -y pulseaudio pulseaudio-utils xvfb

# Start PulseAudio
pulseaudio --start --system=false

# Create virtual audio devices
pactl load-module module-null-sink sink_name=koov_input sink_properties=device.description="Koov Input"
pactl load-module module-null-sink sink_name=koov_output sink_properties=device.description="Koov Output"

# Make persistent
echo 'load-module module-null-sink sink_name=koov_input sink_properties=device.description="Koov Input"' | sudo tee -a /etc/pulse/default.pa
echo 'load-module module-null-sink sink_name=koov_output sink_properties=device.description="Koov Output"' | sudo tee -a /etc/pulse/default.pa

# Set up Xvfb for headless browser
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 > /dev/null 2>&1 &

# Verify
pactl list sinks | grep -A 5 koov
```

### 5. Deploy Koov Code

**Option A: Clone from Git**
```bash
git clone https://github.com/yourusername/koovdemo.git
cd koovdemo
```

**Option B: Upload via Azure Storage**
```bash
# On your local machine
az storage blob upload --account-name yourstorage --container-name koov --name koovdemo.zip --file koovdemo.zip

# On VM
az storage blob download --account-name yourstorage --container-name koov --name koovdemo.zip --file koovdemo.zip
unzip koovdemo.zip
```

**Set up Python environment:**
```bash
cd koovdemo
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python3 scripts/download_vosk_model.py --out models
```

### 6. Configure Environment

```bash
nano .env
```

Add your configuration (same as AWS guide):
```bash
KOOV_INPUT_DEVICE=koov_input
KOOV_OUTPUT_DEVICE=koov_output
KOOV_API_HOST=0.0.0.0
KOOV_API_PORT=5000
# ... rest of your config
```

### 7. Set Up Process Manager (PM2)

```bash
sudo npm install -g pm2

cd /home/azureuser/koovdemo
source .venv/bin/activate
export PYTHONPATH="$PWD"
pm2 start scripts/start_api_server.py --name koov-api --interpreter python3
pm2 save
pm2 startup
# Follow instructions
```

### 8. Set Up Nginx

```bash
sudo apt install -y nginx

sudo nano /etc/nginx/sites-available/koov-api
```

Add configuration (same as AWS):
```nginx
server {
    listen 80;
    server_name your-domain.com;

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

Enable:
```bash
sudo ln -s /etc/nginx/sites-available/koov-api /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

### 9. Set Up SSL

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

### 10. Configure Network Security Group

1. Go to Azure Portal → Your VM → Networking
2. Add inbound port rules:
   - SSH (22) - from your IP
   - HTTP (80) - from anywhere
   - HTTPS (443) - from anywhere
   - Custom (5000) - from your dashboard IP (optional)

### 11. Get Your API URL

**With domain:**
```
https://your-domain.com
```

**Without domain:**
```
http://your-vm-public-ip:5000
```

### 12. Update Dashboard

```bash
KOOV_API_URL=https://your-domain.com
```

## Azure Container Instances (Alternative)

### 1. Create Dockerfile

Same as AWS ECS guide.

### 2. Build and Push to Azure Container Registry

```bash
# Create ACR
az acr create --resource-group your-rg --name yourregistry --sku Basic

# Login
az acr login --name yourregistry

# Build and push
az acr build --registry yourregistry --image koov-api:latest .
```

### 3. Deploy Container Instance

```bash
az container create \
  --resource-group your-rg \
  --name koov-api \
  --image yourregistry.azurecr.io/koov-api:latest \
  --cpu 2 \
  --memory 4 \
  --registry-login-server yourregistry.azurecr.io \
  --registry-username yourregistry \
  --registry-password your-password \
  --ports 5000 \
  --ip-address Public \
  --environment-variables \
    KOOV_API_HOST=0.0.0.0 \
    KOOV_API_PORT=5000
```

### 4. Get Public IP

```bash
az container show --resource-group your-rg --name koov-api --query ipAddress.ip -o tsv
```

## Cost Estimation

**VM:**
- Standard_B2s: ~$30/month (2-3 meetings)
- Standard_B2ms: ~$60/month (5-10 meetings)
- Standard_B4ms: ~$120/month (10+ meetings)

**Container Instances:**
- Similar pricing to VMs
- Pay per second of usage

**Additional:**
- Data transfer: ~$0.05/GB (first 5 GB free)
- Storage: ~$0.10/GB-month

## Monitoring

**Azure Monitor:**
- CPU, memory, network metrics
- Set up alerts
- View logs in Log Analytics

**PM2:**
```bash
pm2 monit
pm2 logs koov-api
```

## Troubleshooting

**Connection issues:**
- Check Network Security Group rules
- Verify VM is running
- Check Nginx: `sudo systemctl status nginx`

**Audio issues:**
- Verify PulseAudio: `pactl list sinks`
- Check .env configuration

**PM2 issues:**
- Check logs: `pm2 logs koov-api`
- Verify environment variables

## Azure-Specific Tips

1. **Use Azure Key Vault** for secrets:
   ```bash
   az keyvault create --name your-keyvault --resource-group your-rg
   az keyvault secret set --vault-name your-keyvault --name koov-eleven-api-key --value your-key
   ```

2. **Set up Auto-shutdown** to save costs (dev/test):
   - VM → Auto-shutdown → Enable

3. **Use Azure Bastion** for secure SSH (no public IP needed)

4. **Backup with Azure Backup**

## Next Steps

1. Test deployment
2. Monitor costs in Azure Cost Management
3. Set up alerts
4. Scale VM size if needed
5. Consider Azure Load Balancer for multiple VMs
