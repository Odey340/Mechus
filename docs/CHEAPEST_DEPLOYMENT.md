# Cheapest Way to Deploy Koov

This guide shows the absolute cheapest options for deploying Koov to the cloud.

## Cheapest Options Ranked

### 1. AWS Free Tier (FREE for 12 months) ⭐ Best Deal
- **Cost:** $0/month for first year
- **What you get:** t2.micro or t3.micro (1 vCPU, 1 GB RAM)
- **Limitation:** Only 1 meeting at a time, may be slow
- **After 12 months:** ~$8-10/month

### 2. DigitalOcean Droplet ($6/month) ⭐ Best Value
- **Cost:** $6/month
- **What you get:** 1 vCPU, 1 GB RAM, 25 GB storage
- **Good for:** 1-2 meetings
- **No free tier, but very cheap**

### 3. Linode Nanode ($5/month)
- **Cost:** $5/month
- **What you get:** 1 vCPU, 1 GB RAM, 25 GB storage
- **Similar to DigitalOcean**

### 4. Vultr ($6/month)
- **Cost:** $6/month
- **What you get:** 1 vCPU, 1 GB RAM, 25 GB storage
- **Similar pricing**

### 5. Azure Free Tier (FREE for 12 months) ⭐ Also FREE
- **Cost:** $0/month for first year
- **What you get:** B1S (1 vCPU, 1 GB RAM)
- **Similar to AWS free tier**
- **Good alternative if you prefer Azure over AWS**

## Recommendation: Start with AWS Free Tier

**Why:**
- FREE for 12 months
- Enough for testing and 1 meeting
- Easy to upgrade later
- Good documentation

## Step-by-Step: AWS Free Tier (FREE)

### 1. Sign Up for AWS

1. Go to https://aws.amazon.com
2. Create account (need credit card, but won't be charged if you stay in free tier)
3. Verify identity

### 2. Launch Free Tier EC2 Instance

1. **AWS Console** → EC2 → Launch Instance

2. **Choose:**
   - **AMI:** Ubuntu Server 22.04 LTS (Free tier eligible)
   - **Instance Type:** `t2.micro` or `t3.micro` (FREE tier eligible)
   - **Key Pair:** Create new, download `.pem` file
   - **Network Settings:**
     - Allow SSH (22) from your IP
     - Allow HTTP (80) from anywhere
     - Allow HTTPS (443) from anywhere
     - Allow Custom TCP (5000) from anywhere
   - **Storage:** 8 GB (free tier limit)

3. **Launch Instance**

**Important:** Make sure you see "Free tier eligible" badge!

### 3. Connect and Set Up (Same as Full Guide)

Follow steps 3-12 from `DEPLOY_AWS.md`, but note:

**Limitations:**
- Only 1 GB RAM - may be slow
- Only 1 meeting at a time
- May need to optimize memory usage

**Optimizations for Free Tier:**
```bash
# Use lighter models
KOOV_WHISPER_MODEL=tiny.en  # Instead of base.en
KOOV_VOSK_MODEL_PATH=./models/vosk-model-small-en-us-0.15  # Keep small

# Reduce memory usage
KOOV_MEMORY_MAX_CHARS=10000  # Instead of 20000
KOOV_MEMORY_RECENT_CHARS=2000  # Instead of 4000
```

### 4. Monitor Free Tier Usage

1. Go to AWS Console → Billing Dashboard
2. Set up billing alerts (get email if you exceed free tier)
3. Monitor usage to stay within limits

**Free Tier Limits:**
- 750 hours/month of t2.micro or t3.micro
- 30 GB storage
- 15 GB data transfer out

## Alternative: DigitalOcean ($6/month)

If you want to skip free tier limitations:

### 1. Sign Up

1. Go to https://www.digitalocean.com
2. Create account
3. Add payment method (get $200 credit with referral)

### 2. Create Droplet

1. **Create** → **Droplets**

2. **Choose:**
   - **Image:** Ubuntu 22.04
   - **Plan:** Basic - $6/month (1 vCPU, 1 GB RAM)
   - **Datacenter:** Closest to you
   - **Authentication:** SSH keys (recommended)
   - **Hostname:** koov-api

3. **Create Droplet**

### 3. Deploy (Same Steps)

Follow the same deployment steps as AWS/Azure, but simpler:
- No complex security groups
- Direct IP access
- Simpler setup

## Cost Comparison

| Provider | First Year | After First Year | Best For |
|----------|-----------|------------------|----------|
| **AWS Free Tier** | **$0** | $8-10/month | Testing, learning, most popular |
| **Azure Free Tier** | **$0** | $8-10/month | Testing, learning, Microsoft ecosystem |
| **DigitalOcean** | $6/month | $6/month | Production, simplicity |
| **Linode** | $5/month | $5/month | Cheapest paid option |
| **Vultr** | $6/month | $6/month | Similar to DO |

## Minimizing Costs

### 1. Use Free Tier First
- Test with AWS/Azure free tier
- Upgrade only when needed

### 2. Auto-Shutdown (Dev/Test)
```bash
# Azure: Enable auto-shutdown in portal
# AWS: Use Lambda to stop instance during off-hours
```

### 3. Use Reserved Instances (After Free Tier)
- AWS: Save up to 72% with 1-year commitment
- Azure: Save up to 72% with 1-year commitment

### 4. Optimize Resource Usage
- Use smaller models (tiny.en instead of base.en)
- Reduce memory settings
- Only run when needed

### 5. Use Spot Instances (Advanced)
- AWS Spot: Up to 90% discount
- Azure Spot: Up to 90% discount
- **Warning:** Can be terminated, not for production

## Quick Start: Cheapest Option

### Option A: AWS Free Tier (FREE)

```bash
# 1. Launch t2.micro instance (free tier)
# 2. SSH in
ssh -i your-key.pem ubuntu@your-ec2-ip

# 3. Quick setup
sudo apt update && sudo apt install -y python3.11 python3-pip git nodejs
git clone your-repo
cd koovdemo
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Install PM2
sudo npm install -g pm2

# 5. Start
export PYTHONPATH="$PWD"
pm2 start scripts/start_api_server.py --interpreter python3
pm2 save
pm2 startup
```

### Option B: DigitalOcean ($6/month)

Same steps, but:
- Simpler setup (no security groups)
- Direct IP access
- Slightly faster

## Cost Breakdown

**AWS Free Tier (First Year):**
- EC2: $0
- Storage: $0 (first 30 GB)
- Data transfer: $0 (first 15 GB)
- **Total: $0/month**

**After Free Tier:**
- t2.micro: ~$8-10/month
- Storage: ~$1/month
- Data transfer: ~$1/month
- **Total: ~$10-12/month**

**DigitalOcean:**
- Droplet: $6/month
- Storage: Included
- Data transfer: Included (1 TB)
- **Total: $6/month**

## Recommendation

**For absolute cheapest (FREE):**
1. **Start with AWS Free Tier** (FREE for 12 months) - Most popular, best docs
   - OR
2. **Start with Azure Free Tier** (FREE for 12 months) - If you prefer Microsoft ecosystem
3. **After 12 months, switch to DigitalOcean** ($6/month) or Linode ($5/month)

**For simplicity (paid):**
- **Go straight to DigitalOcean** ($6/month)
- No free tier complexity
- Simpler setup
- Predictable pricing

**Azure vs AWS Free Tier:**
- **AWS:** More popular, better documentation, larger community
- **Azure:** Better if you use Microsoft services, Office 365, Azure AD
- **Both are FREE for 12 months** - choose based on preference

## Next Steps

1. Choose your option (AWS Free Tier recommended)
2. Follow deployment guide
3. Set up billing alerts
4. Monitor usage
5. Optimize for cost

## Important Notes

**Free Tier Limitations:**
- Limited to 1 meeting at a time
- May be slow with 1 GB RAM
- Need to monitor usage carefully
- Will be charged if you exceed limits

**When to Upgrade:**
- Need multiple meetings
- Performance is too slow
- Running out of free tier hours
- Ready for production
