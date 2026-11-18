# COE 548 AIaaS Platform (Team 5)

A self-service API platform for OpenAI's open-weight **gpt-oss-20b**. Users sign up on a web portal, create an API key, and call the model from their own code through an OpenAI-compatible API, with per-key rate limits and usage metering.

Live at: https://coe548.spaincentral.cloudapp.azure.com

## How it fits together

```
                      ┌──────────────── Azure VM (always on, Docker Compose) ────────────────┐
User ──HTTPS──> Caddy ─┬─ /v1/chat/completions, /v1/models ──> LiteLLM gateway ──> vLLM on a RunPod GPU pod
                       │                                          │                (on demand)
                       └─ everything else ──> Portal (FastAPI) ───┤
                                                  │               │
                                                  └──> PostgreSQL <┘   (two databases: portal, litellm)
```

- **Caddy**: TLS and routing. Only the two model routes reach the gateway; every other `/v1` path gets a 404.
- **Portal** (`portal/`): accounts, API keys, usage, a chat playground and the API docs. It talks to the gateway's admin API on the internal network.
- **LiteLLM gateway**: checks keys, enforces rate limits, records usage.
- **vLLM on RunPod**: serves the model. Started only when needed. While no pod is running the API returns an error, the portal shows "GPU offline", and everything else stays up.

## What works

| Area | State |
|---|---|
| Model served on a RunPod GPU through vLLM | Done |
| Gateway over HTTPS, API keys, per-key rate limits (429 over the limit), token usage per key | Done |
| Portal: sign-up with an invite code, sign-in, create and revoke API keys | Done |
| Portal: usage per day and per key, CSV export | Done |
| Portal: chat playground (streaming, shows the model's reasoning) | Done |
| API documentation with curl, Python and JavaScript examples (`/docs`) | Done |
| Monitoring (Prometheus, Grafana), second GPU replica, load tests | Not built yet |
| Model pricing in the gateway, for cost per million tokens | Not built yet |

## Files

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The services on the VM |
| `Caddyfile` | HTTPS and routing |
| `config.yaml` | LiteLLM model list and settings |
| `portal/` | The portal: FastAPI app, templates, tests, Dockerfile |
| `scripts/deploy.sh` | Deploys the current checkout on the VM |
| `scripts/e2e.py` | End-to-end check through the public URL |
| `.env.example` | Names of the required settings. Real values are in `.env` on the VM |
| `docker-compose.dev.yml`, `Caddyfile.dev`, `dev/mock_vllm/` | Local development: a mock model, so no GPU is needed |

## The portal

| Page | What it does |
|---|---|
| `/` | Landing page with the model's live status |
| `/signup`, `/login` | Accounts. Sign-up needs the invite code (`PORTAL_INVITE_CODE` in `.env`) |
| `/overview` | Today's requests and tokens, a 14-day chart, quickstart |
| `/keys` | Create a key (shown once) and revoke keys. Up to 5 per user |
| `/usage` | Totals, daily chart, per-key breakdown, recent requests, CSV export |
| `/chat` | Chat with the model. Conversations are kept in the browser |
| `/docs` | API documentation |

Each new key gets 10 requests and 30,000 tokens a minute; each account 30 requests and 60,000 tokens a minute across its keys. These defaults are `PORTAL_*` settings in `docker-compose.yml`.

The first account to sign up becomes the admin. To change roles later:

```bash
docker compose exec portal python -m app.cli list-users
docker compose exec portal python -m app.cli promote someone@example.com
```

## Working on the VM

The stack lives in `~/aiaas`. Real secrets are in `~/aiaas/.env` there and are never committed.

```bash
cd ~/aiaas
git pull && scripts/deploy.sh     # deploy: backs up, rebuilds, restarts what changed, self-checks
docker compose ps                 # what is running
docker compose logs -f portal     # follow a service's logs
docker compose exec portal python -m app.cli selfcheck
```

`scripts/deploy.sh` is safe to run repeatedly. It writes a backup of both databases and `.env` to `~/aiaas-backups/` first, and prints the invite code at the end.

There is one live stack shared by all of us. Commit every change with a clear message.

## Using the API

Create a key on the portal, then:

```bash
curl https://coe548.spaincentral.cloudapp.azure.com/v1/chat/completions \
  -H "Authorization: Bearer sk-YOUR-KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss-20b","messages":[{"role":"user","content":"Say hello"}]}'
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://coe548.spaincentral.cloudapp.azure.com/v1",
    api_key="sk-YOUR-KEY",
)
r = client.chat.completions.create(
    model="gpt-oss-20b",
    messages=[{"role": "user", "content": "Say hello"}],
)
print(r.choices[0].message.content)
```

The full reference, with streaming, rate limits and errors, is at `/docs` on the site.

## The kinds of keys

| Key | Used by | Where it lives |
|---|---|---|
| User keys (`sk-...`) | Users, sent to the public URL | Created on the portal's API keys page |
| Master key | The portal and admins, on the internal network only | `LITELLM_MASTER_KEY` in `.env` |
| Pod key | Only the gateway, to call vLLM | `VLLM_API_KEY` in `.env` |

Users never see the pod URL or the pod key. The gateway's admin API is not reachable from the internet.

## GPU pod

The pod costs money while it runs. Terminate it when nobody is testing. While no pod is running, the API returns an error and the portal shows "GPU offline"; the rest of the platform stays up.

To start one: RunPod > Pods > Deploy, and use:

- Image: `vllm/vllm-openai:v0.34.0`
- HTTP port: `8000`
- Volume mount path: `/workspace`, environment variable `HF_HOME=/workspace/hf`
- Start command:

```
--host 0.0.0.0 --port 8000 --model openai/gpt-oss-20b --served-model-name gpt-oss-20b --gpu-memory-utilization 0.90 --max-model-len 32768 --api-key <POD_KEY>
```

A new pod gets a new address. Update the VM and apply:

```bash
nano ~/aiaas/.env     # set VLLM_API_BASE=https://<pod-id>-8000.proxy.runpod.net/v1 and VLLM_API_KEY
docker compose up -d
```

## Local development

No GPU needed: a small mock model stands in for vLLM.

```bash
cp .env.example .env    # fill in local values; PORTAL_INVITE_CODE is overridden to "local-invite"
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
# portal: http://localhost:8088    gateway admin: http://127.0.0.1:4000

BASE_URL=http://localhost:8088 INVITE_CODE=local-invite python3 scripts/e2e.py   # end-to-end check
cd portal && uv venv .venv --python 3.13 && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q                                                    # unit tests
```

## Rules

- Never commit `.env` or paste real keys in chat or in code.
- Do not change `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY` or `PORTAL_SECRET_KEY`; existing data depends on them.
