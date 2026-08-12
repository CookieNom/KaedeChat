# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.8 AS uv
FROM python:3.12.13-slim AS tooling
COPY --from=uv /uv /uvx /bin/
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libvips42 \
    && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /workspace

FROM tooling AS dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

FROM dependencies AS development
COPY . .
RUN uv sync --locked
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--ws-max-size", "1048576", "--no-access-log"]

FROM tooling AS production-dependencies
COPY pyproject.toml uv.lock ./
COPY app app
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.13-slim AS runtime
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libvips42 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=production-dependencies /opt/venv /opt/venv
COPY app app
COPY alembic.ini ./
COPY migrations migrations
COPY scripts scripts
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin kaede
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER kaede
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "1048576", "--no-access-log"]
