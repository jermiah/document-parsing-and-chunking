# syntax=docker/dockerfile:1
# Refresh this verified multi-platform digest deliberately when updating Python.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS system

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

FROM system AS builder
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.9.10

# Cache third-party dependencies independently of application source changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra docling --no-install-project
# Fail the build if the native vision dependencies cannot load.
RUN .venv/bin/python -c "from transformers import AutoImageProcessor; from torchvision.ops import nms; import torch; nms(torch.zeros((0, 4)), torch.zeros(0), 0.5)"
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra docling --no-editable

# Only the installed environment and runtime files enter the application image.
FROM system AS base
ENV PATH="/app/.venv/bin:$PATH" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY --from=builder /app/.venv /app/.venv
COPY configs ./configs
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

# Keep application code root-owned; only output folders need to be writable.
RUN groupadd --gid 10001 ingestion \
    && useradd --uid 10001 --gid 10001 --create-home ingestion \
    && mkdir -p /app/data /app/artifacts /app/reports /app/models \
    && chown ingestion:ingestion /app/data /app/artifacts /app/reports /app/models
USER ingestion
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).close()"]
# exec forwards Docker's stop signal to Uvicorn; migration failure prevents startup.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn ingestion.api:create_app --factory --host 0.0.0.0 --port 8000"]

FROM base AS benchmark
# Compatibility target; the base runtime now includes the automatic OCR fallback.

