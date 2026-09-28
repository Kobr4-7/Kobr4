# 1. Site web
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# 2. Application Python
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY config/ config/
RUN uv sync --locked --no-dev
COPY --from=web /web/dist /app/static
RUN useradd --system --uid 1000 --home /app kobr4 && mkdir -p /data /lab && chown -R kobr4 /data /lab
USER kobr4
ENV PATH="/app/.venv/bin:$PATH" \
    KOBR4_STATIC_DIR=/app/static \
    KOBR4_DATA_DIR=/data \
    KOBR4_LAB_DIR=/lab
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["kobr4", "server", "run", "--host", "0.0.0.0", "--port", "8000"]
