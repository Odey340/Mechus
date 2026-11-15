# Production Deployment (Cloud)

Mechus is production-grade and is intended to run in Docker (or cloud containers), not as a local desktop process. This guide covers security, configuration, scaling, and deployment.

---

## 1. Security

### API authentication (required when exposed)

When the API is reachable from the internet, **always** set an API key so only your dashboard (or backend) can call it.

- **Set:** `MECHUS_API_KEY=<strong-secret>` (e.g. from your secrets manager).
- **Clients:** Send the key on every request:
  - Header: `X-API-Key: <your-key>`, or
  - Header: `Authorization: Bearer <your-key>`.
- **Public routes:** `/health` and `/ready` do **not** require the key (for load balancers and health checks).

### CORS

Restrict origins in production so only your dashboard can call the API.

- **Set:** `MECHUS_CORS_ORIGINS=https://your-dashboard.example.com,https://app.example.com` (comma-separated).
- **Omit:** In development you can leave this unset (all origins allowed).

### Secrets

- **Never** commit `.env` or API keys to the repo.
- In production, inject secrets via your platform:
  - **AWS:** Secrets Manager, Parameter Store, or ECS task env.
  - **GCP:** Secret Manager or Cloud Run env.
  - **Azure:** Key Vault or App Service config.
  - **Kubernetes:** Secrets or External Secrets.
- Required secrets for full functionality: `MECHUS_API_KEY`, Zoom (`ZOOM_APP_CLIENT_ID`, `ZOOM_APP_CLIENT_SECRET`), TTS/LLM keys as needed.

### HTTPS

- Terminate TLS at the load balancer or ingress (recommended). Run Mechus on HTTP inside the network (e.g. `MECHUS_API_HOST=0.0.0.0`).
- Do not put secrets in the URL; use headers and env.

---

## 2. Configuration (environment)

| Variable | Description | Production |
|----------|-------------|------------|
| `MECHUS_API_KEY` | API key; if set, required on all API routes except /health, /ready | **Set** (strong secret) |
| `MECHUS_CORS_ORIGINS` | Allowed origins (comma-separated) | **Set** (dashboard origin(s)) |
| `MECHUS_API_HOST` | Bind address | `0.0.0.0` (when in container/VM) |
| `MECHUS_API_PORT` | Port | `5000` (or platform default) |
| `MECHUS_LOG_LEVEL` | Log level | `INFO` (or `WARNING` to reduce noise) |
| `MECHUS_ENV_FILE` | Path to .env (optional) | Omit if using platform env |
| Zoom / TTS / LLM | See README and ZOOM_SDK_SETUP | From secrets manager |

---

## 3. Running the server (production)

### Gunicorn (recommended)

Do **not** use the Flask dev server in production. Use gunicorn:

```bash
pip install -r requirements.txt -r requirements-prod.txt
export PYTHONPATH=/path/to/mechusdemo
gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - mechus.api_server:app
```

- **Workers:** Use **1** worker when using browser or Zoom SDK join (sessions are in-memory per process). Scale by running more instances (horizontal scaling) instead of more workers.
- **Threads:** 4 is usually enough for the API.
- **Timeout:** 120s allows long-running join/transcript operations.

### Docker (production image)

The repo Dockerfile is production-oriented: non-root user, gunicorn, healthcheck.

```bash
docker build -t mechus-api .
docker run -p 5000:5000 --env-file .env mechus-api
```

Use `docker compose up` for local or single-node deployment; see `docker-compose.yml` for healthcheck and restart policy. After the container is up, run `./scripts/test-docker-api.sh` to smoke-test `/health` and `/api/join-meeting` (and confirm the wake-word model is found).

---

## 4. Health checks

- **Liveness:** `GET /health` – process is up. Use for orchestrator restarts.
- **Readiness:** `GET /ready` – ready to accept traffic. Use for load balancer targets.

Both return 200 and JSON; no API key required. Configure your load balancer or Kubernetes to use these.

---

## 5. Scaling (e.g. 100 users / 100 meetings)

- **Sessions:** Stored in memory per process. One worker = one process = one set of active meetings per instance. For **current** code (one process, one thread per meeting, STT/LLM/TTS in-process), capacity is ~2–4 meetings on 4/8 and ~6–10 on 8/16. For **30–50 meetings per server** you must use the dumb-bot + scheduler design: see **[SCALABLE_BOT_ARCHITECTURE.md](SCALABLE_BOT_ARCHITECTURE.md)**. See **[CAPACITY_AND_COST.md](CAPACITY_AND_COST.md)** for cost per meeting and provider comparison.
- **Many users:** To support many concurrent meetings (e.g. 100), run **multiple instances** (e.g. 10–20 containers or VMs) behind a load balancer so total capacity is enough (e.g. 5–10 meetings per instance). Optimize for **cost per simultaneous meeting**, not cheapest server.
- **Sticky sessions (required):** The load balancer **must** use session affinity so that all requests for the same user or meeting go to the **same** instance. Otherwise “join” may create the session on instance A, but “status” or “stop” can hit instance B, which has no session and will return an error.
  - **AWS ALB:** Enable stickiness on the target group (e.g. cookie-based or duration-based).
  - **Azure:** Use Application Gateway or Load Balancer with session affinity (client IP or application cookie).
- **Zoom SDK:** Only available in Linux containers/VMs; use Docker on Mac/Windows for Zoom join. For cloud, run the same image on Linux (e.g. ECS, Cloud Run, AKS).

---

## 6. Logging and observability

- **Logs:** Gunicorn logs to stdout/stderr (e.g. `--access-logfile - --error-logfile -`). In cloud, capture stdout/stderr with your log agent (CloudWatch, Stackdriver, Azure Monitor, etc.).
- **Log level:** `MECHUS_LOG_LEVEL=INFO` (or `WARNING`). Avoid `DEBUG` in production (PII in logs).
- **Metrics:** Optional: add a Prometheus `/metrics` endpoint or use your platform’s APM. Not included by default.

---

## 7. Data and privacy

- **Transcripts/notes:** Stored under `mechus_notes/` (or configured root) on the instance’s disk. Plan retention and backup per your policy; restrict filesystem permissions.
- **PII:** Logs are redacted at INFO level (see DATA_PRIVACY.md). Do not ship DEBUG logs to shared systems.
- **Compliance:** Ensure your deployment meets your data residency and access controls (encryption, network, secrets).

---

## 8. Checklist before going live

- [ ] `MECHUS_API_KEY` set and sent by dashboard (or backend).
- [ ] `MECHUS_CORS_ORIGINS` set to dashboard origin(s).
- [ ] API not exposed to the internet without TLS (terminate at LB/ingress).
- [ ] Secrets from a secrets manager, not in code or in repo.
- [ ] Server run with gunicorn (or production Docker image).
- [ ] Health/ready checks configured on load balancer or orchestrator.
- [ ] Log level `INFO` or `WARNING`; logs captured for debugging.
- [ ] Session/transcript retention and disk permissions reviewed.

---

## 9. Example: single instance on a cloud VM

For **what to build first** (Linux, recording token, streaming TTS, VAD), see **[IMPLEMENTATION_PRIORITY.md](IMPLEMENTATION_PRIORITY.md)**. For capacity and cost per meeting (e.g. Hetzner vs DigitalOcean vs AWS), see **[CAPACITY_AND_COST.md](CAPACITY_AND_COST.md)**.

1. **VM:** Linux (e.g. Ubuntu 22.04) on AWS/GCP/Azure or Hetzner/DO.
2. **Install:** Docker (or Python 3.10+ and deps).
3. **Secrets:** Store `MECHUS_API_KEY`, Zoom credentials, TTS/LLM keys in platform secrets; inject as env.
4. **Run:** `docker run -d -p 5000:5000 --env-file /path/to/env --restart unless-stopped mechus-api` (or use docker compose).
5. **LB:** Put the VM behind a load balancer; TLS at LB; allow only `/health`, `/ready`, and `/api/*` to the VM.
6. **Dashboard:** Call `https://your-api.example.com/api/join-meeting` with `X-API-Key` and `MECHUS_CORS_ORIGINS=https://your-dashboard.example.com`.

For multi-instance, run the same image on multiple VMs/containers and put them behind the same load balancer; use one instance per meeting or sticky sessions as needed.
