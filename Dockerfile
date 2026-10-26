# The FastAPI agent plus the React page, one image.
#   docker build -t ddt-copilot .     (needs data/articles.jsonl: `make corpus` or the corpus-v1 release)

FROM node:24.22.1-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY web/ ./
RUN npm run build

FROM python:3.12.15-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

FROM python:3.12.15-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY copilot/ copilot/
COPY retrieval/ retrieval/
COPY ingest/ ingest/
COPY data/articles.jsonl data/articles.jsonl
COPY --from=web /web/dist web/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "copilot.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
