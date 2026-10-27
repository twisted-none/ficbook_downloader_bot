FROM python:3.14.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-liberation xvfb xauth libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    libxss1 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

COPY browser/requirements.lock /app/browser/requirements.lock
RUN python -m venv /opt/browser \
    && /opt/browser/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes -r /app/browser/requirements.lock \
    && /opt/browser/bin/pip check
COPY browser/install_browser.py /tmp/install_browser.py
RUN --mount=type=cache,id=janitor-camoufox-probe-archive,target=/opt/browser-asset-cache \
    python /tmp/install_browser.py && rm /tmp/install_browser.py
RUN /opt/browser/bin/python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"

COPY src/core/__init__.py src/core/limits.py /app/src/core/
COPY src/sources/__init__.py src/sources/safe_http.py /app/src/sources/
COPY browser/run.py browser/server.py browser/transport.py /app/browser/
RUN useradd --create-home --uid 10001 browser
USER 10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]
CMD ["python", "-I", "/app/browser/server.py"]
