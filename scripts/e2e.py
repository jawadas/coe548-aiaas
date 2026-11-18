#!/usr/bin/env python3
"""End-to-end check of the whole platform through its public URL.

Signs a new user up on the portal, creates an API key, calls the model with it
(plain and streaming), runs into the rate limit, sees the usage appear, chats in
the playground, then revokes the key and confirms it stops working.

    BASE_URL=http://localhost:8088 INVITE_CODE=local-invite python3 scripts/e2e.py

Standard library only. It creates one account (e2e-<time>@example.test) and
leaves it behind; use it against a local or test stack.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8088").rstrip("/")
INVITE = os.environ.get("INVITE_CODE", "local-invite")
MODEL = os.environ.get("MODEL", "gpt-oss-20b")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}", flush=True)


def call(method: str, path: str, *, form=None, body=None, key=None, headers=None, timeout=90):
    """One request. Returns (status, lower-cased headers, text). Portal requests carry the session cookie."""
    data = None
    request_headers = {"Origin": BASE, **(headers or {})}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    if key is not None:
        request_headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=request_headers)
    try:
        with (opener if key is None else urllib.request.build_opener()).open(request, timeout=timeout) as response:
            return response.status, _lower(response.headers), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, _lower(error.headers), error.read().decode()


def _lower(headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else ""


def chat(key: str, text: str, **extra):
    payload = {"model": MODEL, "messages": [{"role": "user", "content": text}], **extra}
    return call("POST", "/v1/chat/completions", body=payload, key=key)


def main() -> int:
    print(f"Target: {BASE}\n")

    # -- public pages and routing ------------------------------------------
    for path in ("/", "/docs", "/login", "/healthz"):
        status, _, _ = call("GET", path)
        check(f"GET {path}", status == 200, f"HTTP {status}")
    status, _, text = call("GET", "/v1/model/info", key="sk-anything")
    check("gateway internals are not reachable (/v1/model/info)", status == 404, f"HTTP {status}")
    status, _, _ = call("GET", "/v1/models")
    check("/v1/models needs a key", status == 401, f"HTTP {status}")

    # -- sign up -----------------------------------------------------------
    email = f"e2e-{int(time.time())}@example.test"
    _, _, page = call("GET", "/signup")
    status, _, page = call(
        "POST",
        "/signup",
        form={"csrf_token": csrf(page), "name": "E2E Tester", "email": email,
              "password": f"e2e-{time.time_ns()}", "invite_code": INVITE},
    )
    check("sign up and land on the overview", status == 200 and "Welcome" in page, f"HTTP {status}")

    # -- create a key ------------------------------------------------------
    _, _, page = call("GET", "/keys")
    status, _, page = call("POST", "/keys", form={"csrf_token": csrf(page), "name": "E2E key"})
    secret = re.search(r'id="new-key-secret">(sk-[^<]+)<', page)
    check("create an API key, shown once", status == 200 and secret is not None, f"HTTP {status}")
    if not secret:
        return 1
    key = secret.group(1)
    token = re.search(r'action="/keys/([0-9a-f]{64})/revoke"', page).group(1)
    _, _, again = call("GET", "/keys")
    check("the secret is not shown a second time", key not in again)

    # -- use the key -------------------------------------------------------
    status, _, text = call("GET", "/v1/models", key=key)
    check("GET /v1/models lists the model", status == 200 and MODEL in text, f"HTTP {status}")
    status, headers, text = chat(key, "Say hello in five words.")
    answer = json.loads(text)["choices"][0]["message"] if status == 200 else {}
    check("chat completion returns an answer", status == 200 and bool(answer.get("content")), f"HTTP {status}")
    if status != 200:
        print("      " + text[:300])
    limit = headers.get("x-ratelimit-api_key-limit-requests")
    check("rate-limit headers are present", limit is not None, f"limit {limit}/min")

    status, _, text = chat(key, "Count to three.", stream=True, stream_options={"include_usage": True})
    lines = [line for line in text.splitlines() if line.startswith("data: ")]
    check("streaming ends with [DONE]", status == 200 and bool(lines) and lines[-1] == "data: [DONE]",
          f"{len(lines)} events")

    hit = None
    for _ in range(int(limit or 10) + 2):
        status, headers, text = chat(key, "ping", max_tokens=16)
        if status == 429:
            hit = headers.get("retry-after")
            break
    check("going over the limit returns 429 with Retry-After", hit is not None, f"retry-after {hit}s")

    # -- usage -------------------------------------------------------------
    seen = False
    for _ in range(20):
        _, _, page = call("GET", "/usage?range=7")
        if "E2E key" in page:
            seen = True
            break
        time.sleep(3)
    check("usage shows up on the Usage page", seen)
    status, headers, text = call("GET", "/usage/export.csv?range=7")
    check("usage exports as CSV", status == 200 and text.startswith("date,requests"), f"HTTP {status}")

    # -- playground --------------------------------------------------------
    _, _, page = call("GET", "/chat")
    token_meta = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    status, _, text = call(
        "POST", "/api/chat",
        body={"messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 256},
        headers={"X-CSRF-Token": token_meta},
    )
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    kinds = [event["type"] for event in events]
    check("playground streams an answer", status == 200 and "content" in kinds and kinds[-1] == "done",
          ", ".join(sorted(set(kinds))))
    status, _, _ = call("POST", "/api/chat", body={"messages": [{"role": "user", "content": "x"}]})
    check("playground refuses a request without the CSRF token", status == 403, f"HTTP {status}")

    # -- revoke ------------------------------------------------------------
    _, _, page = call("GET", "/keys")
    status, _, page = call("POST", f"/keys/{token}/revoke", form={"csrf_token": csrf(page)})
    check("revoke the key", status == 200 and "was revoked" in page, f"HTTP {status}")
    status, _, _ = chat(key, "still there?")
    check("a revoked key is rejected", status == 401, f"HTTP {status}")

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) FAILED.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
