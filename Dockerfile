FROM python:3.13-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# libopenslide-dev is NOT required — tiffslide is pure Python.
# We only need the compression libs used by tifffile/Pillow.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-turbo-progs \
    libzstd-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer — only reruns when pyproject.toml/uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project
COPY app/ ./app/
RUN uv sync --frozen --no-dev

# Add venv to PATH so binaries are available without `uv run`
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --no-create-home --shell /bin/false appuser

# Volume mount point for the SQLite annotation database
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

EXPOSE 8080

# workers = 2*CPU+1 is a common heuristic; tune MAX_OPEN_SLIDES to match
# available RAM (each open SVS ≈ 50–200 MB).
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "4", \
     "--preload", \
     "--timeout", "120", \
     "--log-level", "info"]
