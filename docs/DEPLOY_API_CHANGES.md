# Deploy Koov API changes to the VM

Use this when you’ve changed Koov API code (e.g. `koov/api_server.py`) and want it running on your Ubuntu server (e.g. EC2).

---

## Easiest: one command from your Mac

1. **Once:** Copy `.deploy.env.example` to `.deploy.env` and set:
   - `DEPLOY_HOST=ubuntu@3.16.207.181` (your VM)
   - `DEPLOY_SSH_KEY=~/Downloads/koov-key.pem` (path to your SSH key)

2. **Every time you change code:**
   ```bash
   ./run.sh deploy
   ```
   This syncs the repo to the VM and runs the restart script there (down + up --build + health check).

---

## Manual: sync then SSH

### 1. Sync code from your Mac to the server

Run this **on your Mac** from the **koovdemo** repo root (replace the key path and host if needed):

```bash
cd /path/to/koovdemo

rsync -avz -e "ssh -i ~/Downloads/koov-key.pem" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  ./ ubuntu@3.16.207.181:~/koovdemo/
```

Use your real key path (e.g. `~/.ssh/koov.pem`) and server IP/hostname instead of `3.16.207.181` if different.

---

## 2. SSH into the server (run the next steps there)

**Important:** The rebuild/restart commands below run **on the VM**, not on your Mac. After `ssh` you’ll be in a shell on the server.

```bash
ssh -i ~/Downloads/koov-key.pem ubuntu@3.16.207.181
```

---

## 3. Rebuild and restart the Koov container (on the server)

**Still on the server** (your prompt will show `ubuntu@...`):

```bash
cd ~/koovdemo
```

Then try **docker compose** (v2, space) first:

```bash
docker compose -f docker-compose.yml -f docker-compose.zoom.yml down
docker compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

If you see `command not found: docker`, Docker isn’t installed on the server. If you see `compose` not found but `docker` works, try **docker-compose** (v1, hyphen):

```bash
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml down
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
```

- `down` stops and removes the current container.  
- `up -d --build` rebuilds the image (so it includes the new code) and starts the container in the background.

---

## 4. Check that it’s running

```bash
curl -s http://127.0.0.1:5001/health
```

You should see: `{"status":"ok",...}`

To follow logs:

```bash
docker-compose -f docker-compose.yml -f docker-compose.zoom.yml logs -f koov
# or: docker logs -f $(docker ps -q -f name=koov)
```

Ctrl+C to stop following logs.

---

## One-liner from Mac (sync + rebuild on server)

From your **Mac** (repo root), sync and then run rebuild on the server in one go:

```bash
cd /path/to/koovdemo
rsync -avz -e "ssh -i ~/Downloads/koov-key.pem" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  ./ ubuntu@3.16.207.181:~/koovdemo/

ssh -i ~/Downloads/koov-key.pem ubuntu@3.16.207.181 \
  "cd ~/koovdemo && docker-compose -f docker-compose.yml -f docker-compose.zoom.yml down && docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build"
```

If your server uses `docker compose` (v2), replace `docker-compose` with `docker compose` in the `ssh` command.
