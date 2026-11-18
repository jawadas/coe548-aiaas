# The AIaaS user portal: a FastAPI app served by one uvicorn worker on port 8000.
#
# Built by Compose (service "portal"). By hand:  docker build -t aiaas-portal ./portal
# The build context is an allow-list, see .dockerignore: requirements.txt and app/.

FROM python:3.13-slim

LABEL org.opencontainers.image.title="AIaaS portal" \
      org.opencontainers.image.description="Sign-up, API keys, usage, playground and docs for the COE 548 AIaaS platform"

# Logs go straight to stdout, Python writes no .pyc files at run time, pip stays quiet.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# The app runs as this unprivileged account. Fixed ids keep ownership predictable.
RUN groupadd --system --gid 10001 portal \
 && useradd --system --uid 10001 --gid portal --no-create-home --home-dir /app \
            --shell /usr/sbin/nologin portal

WORKDIR /app

# Dependencies first: this layer is rebuilt only when requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The code stays owned by root, so the app can read it but never change it.
# chmod makes that hold whatever the umask of the machine that built the image;
# compileall gives a faster start and fails the build on a syntax error, which
# is better than shipping a container that cannot start.
COPY app/ ./app/
RUN chmod -R a+rX /app \
 && python -m compileall -q app

USER portal

EXPOSE 8000

# Liveness only (/healthz has no dependencies). Standard library: the image has no curl.
HEALTHCHECK --interval=15s --timeout=4s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import http.client, sys; c = http.client.HTTPConnection('127.0.0.1', 8000, timeout=3); c.request('GET', '/healthz'); sys.exit(0 if c.getresponse().status == 200 else 1)"]

# Caddy is the only client, so its X-Forwarded-* headers are trusted.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
