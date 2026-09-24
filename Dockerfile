# WhatsApp lead agent — production image.
#
# Build:  docker compose build
# Run:    docker compose up -d
#
# Design notes:
#   * slim base, no compiler toolchain — every pinned dependency ships a
#     prebuilt wheel, so gcc/python3-dev are not needed (keeps the image small
#     and reduces the attack surface).
#   * secrets are NOT baked in. .env and gcreds.json are bind-mounted at run
#     time by docker-compose.yml, so the image itself is safe to rebuild,
#     push or share.
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE — skip .pyc files (smaller image, no stale caches).
# PYTHONUNBUFFERED      — flush logs immediately so `docker compose logs -f`
#                         shows output live instead of in delayed chunks.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl is here purely for the HEALTHCHECK at the bottom of this file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Dependency layer -------------------------------------------------------
# Copied on its own, BEFORE the application code. Docker caches this layer and
# only redoes the (slow) pip install when requirements.docker.txt changes, so
# ordinary code edits rebuild in a couple of seconds.
COPY requirements.docker.txt ./
RUN pip install --no-cache-dir -r requirements.docker.txt

# --- Application layer ------------------------------------------------------
# Deliberately an allow-list (`*.py`) rather than `COPY . .`. This guarantees
# .env, gcreds.json, venv/, __pycache__/ and the *.py.bak.* snapshots can never
# end up inside the image, even by accident.
# NOTE: if the app ever gains non-Python assets (prompt templates, static
# files, a package sub-directory), add an explicit COPY line for them here.
COPY *.py ./

# --- Non-root user ----------------------------------------------------------
# Running as root inside a container is an unnecessary risk. The app's only
# writable location is /app/data (the SQLite database), so that is the single
# directory appuser needs to own.
# The UID is pinned to 10001 on purpose: the host's ./data directory must be
# owned by 10001 for the bind mount to be writable. See DEPLOY.md.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data
USER appuser

# Documents the port. Actual publishing happens in docker-compose.yml.
EXPOSE 8001

# Marks the container unhealthy if /health stops answering, which is visible in
# `docker compose ps`. --start-period gives uvicorn time to boot before any
# failure is counted against it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8001/health || exit 1

# No --reload in production: it doubles memory use, and a stray file change
# would restart the process mid-conversation.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
