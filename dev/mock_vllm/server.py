"""A tiny stand-in for the vLLM pod, for local development and tests.

It speaks just enough of the OpenAI-compatible API that the LiteLLM gateway
talks to: /v1/models, /v1/chat/completions (streaming and non-streaming, with
gpt-oss style reasoning output), /health and /metrics.

Standard library only, so the image is tiny and there is nothing to install.

Special prompts (anywhere in the last user message) to exercise error paths:
  [[error]]   -> HTTP 500
  [[slow]]    -> 10x token delay
  [[long]]    -> a long, markdown-heavy answer
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("MOCK_MODEL", "gpt-oss-20b")
API_KEY = os.environ.get("MOCK_API_KEY", "")
TOKEN_DELAY = float(os.environ.get("MOCK_TOKEN_DELAY_MS", "12")) / 1000.0
PORT = int(os.environ.get("PORT", "8000"))

_lock = threading.Lock()
_stats = {"requests": 0, "running": 0, "prompt_tokens": 0, "generation_tokens": 0}

LONG_ANSWER = """Here is a short tour of what this platform does.

## The request path

1. Your code sends an OpenAI-style request to the **gateway**.
2. The gateway checks your key, your rate limit and your quota.
3. The request is forwarded to **vLLM** running on a GPU pod.

| Layer | Runs on | Job |
|---|---|---|
| Portal | CPU VM | Accounts, keys, usage |
| Gateway | CPU VM | Auth, limits, metering |
| Model | GPU pod | Inference |

```python
from openai import OpenAI

client = OpenAI(base_url="https://example.test/v1", api_key="sk-...")
reply = client.chat.completions.create(
    model="gpt-oss-20b",
    messages=[{"role": "user", "content": "Say hello"}],
)
print(reply.choices[0].message.content)
```

> This answer comes from the local mock model, not from a real GPU.
"""


def _tokens(text: str) -> list[str]:
    """Split text into small pieces that keep their whitespace, like tokens."""
    return re.findall(r"\s*\S+\s*", text) or [text]


def _count(text: str) -> int:
    return max(1, len(re.findall(r"\w+|[^\w\s]", text)))


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return str(content)
    return ""


def _compose(messages: list[dict]) -> tuple[str, str]:
    """Return (reasoning, answer) for a conversation."""
    text = _last_user_text(messages).strip()
    short = text if len(text) <= 120 else text[:117] + "..."
    reasoning = (
        f'The user says: "{short}". This is the local mock model, so I will give '
        "a short, friendly answer and mention that no real GPU is involved."
    )
    if "[[long]]" in text:
        return reasoning, LONG_ANSWER
    if re.search(r"\bcode\b|\bpython\b", text, re.I):
        answer = (
            "Sure. Here is a small example:\n\n"
            "```python\n"
            "def greet(name: str) -> str:\n"
            '    return f"Hello, {name}!"\n\n'
            'print(greet("COE 548"))\n'
            "```\n\n"
            "It defines a function and calls it once. *(Mock model reply.)*"
        )
        return reasoning, answer
    turns = sum(1 for m in messages if m.get("role") == "user")
    answer = (
        f"**Mock reply** to: “{short}”\n\n"
        f"This is turn {turns} of the conversation. The real model, "
        "`gpt-oss-20b`, answers here when a GPU pod is running."
    )
    return reasoning, answer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-vllm/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        print(f"{self.address_string()} {fmt % args}", flush=True)

    # -- helpers ---------------------------------------------------------
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, kind: str) -> None:
        self._json(
            status,
            {"error": {"message": message, "type": kind, "param": None, "code": status}},
        )

    def _authorized(self) -> bool:
        if not API_KEY:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {API_KEY}"

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        path = self._path()
        if path in ("/health", "/ping"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/metrics":
            with _lock:
                snapshot = dict(_stats)
            label = f'{{model_name="{MODEL}"}}'
            body = (
                "# TYPE vllm:num_requests_running gauge\n"
                f"vllm:num_requests_running{label} {snapshot['running']}\n"
                "# TYPE vllm:num_requests_waiting gauge\n"
                f"vllm:num_requests_waiting{label} 0\n"
                "# TYPE vllm:request_success_total counter\n"
                f"vllm:request_success_total{label} {snapshot['requests']}\n"
                "# TYPE vllm:prompt_tokens_total counter\n"
                f"vllm:prompt_tokens_total{label} {snapshot['prompt_tokens']}\n"
                "# TYPE vllm:generation_tokens_total counter\n"
                f"vllm:generation_tokens_total{label} {snapshot['generation_tokens']}\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return self._error(401, "Unauthorized", "authentication_error")
            return self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "vllm",
                            "max_model_len": 32768,
                        }
                    ],
                },
            )
        self._error(404, "Not Found", "not_found_error")

    def do_POST(self) -> None:
        path = self._path()
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._error(404, "Not Found", "not_found_error")
        if not self._authorized():
            return self._error(401, "Unauthorized", "authentication_error")
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "Invalid JSON body", "invalid_request_error")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._error(400, "messages is required", "invalid_request_error")

        text = _last_user_text(messages)
        if "[[error]]" in text:
            return self._error(500, "Mock upstream failure", "internal_server_error")
        delay = TOKEN_DELAY * (10 if "[[slow]]" in text else 1)

        reasoning, answer = _compose(messages)
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
        answer_tokens = _tokens(answer)
        finish_reason = "stop"
        if isinstance(max_tokens, int) and 0 < max_tokens < len(answer_tokens):
            answer_tokens = answer_tokens[:max_tokens]
            answer = "".join(answer_tokens)
            finish_reason = "length"

        prompt_tokens = sum(_count(str(m.get("content", ""))) + 4 for m in messages)
        completion_tokens = _count(reasoning) + _count(answer)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        with _lock:
            _stats["running"] += 1
        try:
            if body.get("stream"):
                include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                self._stream(
                    completion_id, created, reasoning, answer_tokens, finish_reason,
                    usage if include_usage else None, delay,
                )
            else:
                time.sleep(min(2.0, delay * len(answer_tokens)))
                self._json(
                    200,
                    {
                        "id": completion_id,
                        "object": "chat.completion",
                        "created": created,
                        "model": MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": answer,
                                    "reasoning": reasoning,
                                    "reasoning_content": reasoning,
                                },
                                "finish_reason": finish_reason,
                            }
                        ],
                        "usage": usage,
                    },
                )
        finally:
            with _lock:
                _stats["running"] -= 1
                _stats["requests"] += 1
                _stats["prompt_tokens"] += prompt_tokens
                _stats["generation_tokens"] += completion_tokens

    def _stream(self, completion_id, created, reasoning, answer_tokens, finish_reason, usage, delay):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(delta: dict | None, finish: str | None = None, usage_block: dict | None = None):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL,
                "choices": [],
            }
            if delta is not None or finish is not None:
                chunk["choices"] = [
                    {"index": 0, "delta": delta or {}, "finish_reason": finish}
                ]
            if usage_block is not None:
                chunk["usage"] = usage_block
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        try:
            send({"role": "assistant", "content": ""})
            for piece in _tokens(reasoning):
                send({"reasoning": piece, "reasoning_content": piece})
                time.sleep(delay)
            for piece in answer_tokens:
                send({"content": piece})
                time.sleep(delay)
            send({}, finish=finish_reason)
            if usage is not None:
                send(None, usage_block=usage)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream


if __name__ == "__main__":
    print(f"mock-vllm serving {MODEL} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
