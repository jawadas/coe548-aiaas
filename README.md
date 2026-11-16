COE 548 AIaaS Platform (Team 5)

Serves OpenAI's gpt-oss-20b through an OpenAI-compatible API, with per-user API keys, rate limits and usage tracking.

Live at: https://coe548.spaincentral.cloudapp.azure.com

How it fits together
User --HTTPS--> Caddy --/v1/*--> LiteLLM gateway --> vLLM on RunPod (GPU)
                  |                    |
                  +--/* (portal)       +--> PostgreSQL (keys, usage)
Azure VM (always on): Caddy, LiteLLM, PostgreSQL, all in Docker Compose.
RunPod pod (on demand): vLLM serving gpt-oss-20b. Started only when needed.
What works today
Model served on a RunPod GPU, weights stored on a network volume
Gateway reachable over HTTPS with a valid certificate
API keys created through the gateway's admin API
Per-key rate limits (tested: requests over the limit return 429)
Token usage recorded per key
What is not built yet
Area	Owner
Portal: sign-up, login, key issue page, usage page	Omar
API documentation with Python and curl examples	Omar
Gateway: quotas, key-creation flow for the portal, usage reporting	Ammar
Monitoring (Prometheus, Grafana), second GPU replica, load tests	Ali
Files
File	Purpose
docker-compose.yml	The three services on the VM
Caddyfile	HTTPS and routing. Only /v1/* goes to the gateway
config.yaml	LiteLLM model list and settings
.env.example	Names of the required settings. Real values are in .env on the VM
Working on the VM

The stack lives in ~/aiaas on the VM. Real secrets are in ~/aiaas/.env there and are never committed.

bash
cd ~/aiaas
docker compose ps                # what is running
docker compose logs -f litellm   # follow a service's logs
docker compose up -d             # apply changes to compose, config or .env
docker compose restart litellm   # after editing config.yaml only

There is one live stack shared by all of us. Commit every change to docker-compose.yml, Caddyfile or config.yaml with a clear message.

Using the API

Create a key (on the VM; the admin API is not exposed to the internet):

bash
cd ~/aiaas && source .env
curl -s http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"some-user","models":["gpt-oss-20b"],"rpm_limit":5}'

Call the model from anywhere with the key it returns:

bash
curl https://coe548.spaincentral.cloudapp.azure.com/v1/chat/completions \
  -H "Authorization: Bearer sk-USER-KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss-20b","messages":[{"role":"user","content":"Say hello"}]}'

Python:

python
from openai import OpenAI

client = OpenAI(
    base_url="https://coe548.spaincentral.cloudapp.azure.com/v1",
    api_key="sk-USER-KEY",
)
r = client.chat.completions.create(
    model="gpt-oss-20b",
    messages=[{"role": "user", "content": "Say hello"}],
)
print(r.choices[0].message.content)

Check a key's usage (on the VM):

bash
curl -s "http://localhost:4000/key/info?key=sk-USER-KEY" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
The two kinds of keys
Key	Used by	Where it lives
Gateway keys (sk-...)	Users, sent to the public URL	Created with /key/generate
Master key	Admin API only	LITELLM_MASTER_KEY in .env
Pod key	Only the gateway, to call vLLM	VLLM_API_KEY in .env

Users never see the pod URL or the pod key.

GPU pod

The pod costs money while it runs. Terminate it when nobody is testing; the weights stay on the network volume, so the next pod loads in a few minutes. While no pod is running, the API returns an error but the rest of the platform stays up.

To start one: RunPod > Pods > Deploy, attach the network volume, and use:

Image: vllm/vllm-openai:latest
Volume mount path: /workspace
HTTP port: 8000
Environment variable: HF_HOME=/workspace/hf
Start command:
--host 0.0.0.0 --port 8000 --model openai/gpt-oss-20b --served-model-name gpt-oss-20b --gpu-memory-utilization 0.90 --max-model-len 32768 --api-key <POD_KEY>

A new pod gets a new address. Update the VM and apply:

bash
nano ~/aiaas/.env     # set VLLM_API_BASE=https://<pod-id>-8000.proxy.runpod.net/v1
docker compose up -d
Rules
Never commit .env or paste real keys in chat or in code.
Do not change POSTGRES_PASSWORD or LITELLM_MASTER_KEY; existing data depends on them.
