ARG BROWSER_RUNTIME_IMAGE=ficbook-ao3-runtime:156.0.1-beta.36-py3.14
FROM ${BROWSER_RUNTIME_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY src/core/__init__.py src/core/limits.py /app/src/core/
COPY src/sources/__init__.py src/sources/safe_http.py /app/src/sources/
COPY browser/run.py browser/server.py browser/transport.py /app/browser/
USER 10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]
CMD ["python", "-I", "/app/browser/server.py"]
