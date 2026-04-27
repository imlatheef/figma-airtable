# ── Build stage: install dependencies ────────────────────────────────────────
FROM python:3.12-slim AS builder

# Pin uv version for reproducible builds — update this line to upgrade
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /bin/uv

WORKDIR /app

# Compile bytecode for faster container startup
# Copy mode avoids hard-link issues across filesystem layers
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first — this layer is cached until pyproject.toml changes.
# Once you have a uv.lock file (run `uv lock` locally), add --frozen to both
# uv sync calls for fully reproducible builds.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev

# Copy source and install the project itself
COPY src/ src/
RUN uv sync --no-dev


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy the pre-built virtualenv from the builder — no uv needed at runtime
COPY --from=builder /app/.venv .venv/

# Copy application files
COPY src/ src/
COPY fonts/ fonts/
COPY templates.yaml ./

# Put the venv on PATH
# U2NET_HOME tells rembg where to store/find the model
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    U2NET_HOME=/app/.u2net

# Pre-download the rembg U2Net model at build time so it's baked into the
# image and never needs to download at runtime (~170MB, cached in /app/.u2net)
RUN python -c "\
from rembg import new_session; \
print('Downloading U2Net-p (lite) model...'); \
new_session('u2netp'); \
print('Model ready.')"

# Run as non-root for security
RUN useradd --create-home --no-log-init appuser && chown -R appuser /app
USER appuser

CMD ["python", "-m", "airtable_to_figma.poller"]
