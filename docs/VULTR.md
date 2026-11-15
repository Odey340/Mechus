# Vultr integration: develop locally, deploy globally

Mechus uses Vultr in two ways:

1. **Vultr Cloud Compute** hosts the Mechus API and its Zoom bot container (Docker), provisioned with one command.
2. **Vultr Serverless Inference** can power Mechus's answers through its OpenAI-compatible API, instead of OpenAI.

Resources: [sign up](https://mlh.link/vultr-signup) · [Marketplace tutorials](https://mlh.link/vultr-marketplace) · [FAQ](https://mlh.link/vultr-support) · [MLH x Vultr](https://mlh.link/vultr)

## 1. Deploy Mechus to a Vultr server

```bash
# .env.local  (gitignored; loaded after .env)
VULTR_API_KEY=...                 # Vultr console -> Account -> API (allow your IP)

python scripts/vultr_deploy.py regions                 # default region: dfw (Dallas)
python scripts/vultr_deploy.py create --ssh-pubkey ~/.ssh/id_ed25519.pub
./run.sh deploy                                        # rsync code + docker compose up on the server
curl http://<SERVER_IP>:5001/health
```

`create` boots Ubuntu 24.04 with `deploy/vultr/cloud-init.yaml`, which installs Docker and opens ports 22 and 5001. It then writes `.deploy.env` (`DEPLOY_HOST=root@<ip>`), so the existing `./run.sh deploy`, `./run.sh ssh` and `./run.sh ssh-logs` commands work unchanged. The `./run.sh vultr` shortcut runs `create` and then `deploy`.

- On Windows, run `./run.sh deploy` from Git Bash or WSL (it needs `rsync` and `ssh`). `vultr_deploy.py` itself is pure Python and runs anywhere.
- Default plan: `vc2-2c-4gb`. To run faster-whisper on a GPU, pick a Cloud GPU plan: `python scripts/vultr_deploy.py plans --gpu`, then `create --plan <vcg-...>`.
- When you're done, stop billing with `python scripts/vultr_deploy.py destroy <instance-id>`.
- Before exposing the API publicly, set `MECHUS_API_KEY` and `MECHUS_CORS_ORIGINS` in the server `.env` (see `docs/PRODUCTION.md`).

## 2. Use Vultr Serverless Inference for answers

```bash
# .env.local  (gitignored; loaded after .env)
MECHUS_LLM_PROVIDER=vultr
VULTR_INFERENCE_API_KEY=...       # Vultr console -> Serverless Inference
VULTR_INFERENCE_MODEL=<model id>  # list them with the script below

python scripts/vultr_inference_check.py   # lists models + times one test answer
```

Every Chat Completions call in Mechus (spoken answers, thinking layer, summaries, chapters, action items) now goes to `https://api.vultrinference.com/v1`. `response_format=json_object` is turned off for non-OpenAI endpoints (Mechus's prompts still ask for JSON and its parsers already fall back). Force it back on with `MECHUS_LLM_JSON_MODE=1` if your model supports it.

**Self-hosted model on a Vultr Cloud GPU:** run vLLM or Ollama on the GPU server and set `MECHUS_OPENAI_BASE_URL=http://<gpu-ip>:8000/v1`.

Speech-to-text via the OpenAI Whisper API and embeddings still use OpenAI when those backends are selected. The default STT (`faster-whisper`) runs locally on the server.
