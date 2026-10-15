# --- Stage 1: download the OpenCode executable -------------------------------------------------------------------
# OpenCode is published on npm as a small launcher plus a self-contained binary per platform. Node is only needed to
# download it here; the final image gets just the binary (no Node, no npm), so it stays small.
FROM node:22-slim AS opencode
# Pinned on purpose: OpenCode changes quickly. Bump it deliberately and run `python scripts/check_opencode.py` after.
ARG OPENCODE_VERSION=1.19.0
RUN set -eux; \
    npm install -g "opencode-ai@${OPENCODE_VERSION}"; \
    case "$(uname -m)" in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; *) echo "unsupported CPU: $(uname -m)"; exit 1 ;; esac; \
    root="$(npm root -g)"; \
    mkdir -p /out; \
    cp "$(find "$root" -type f -path "*/node_modules/opencode-linux-${arch}/bin/opencode" | head -n 1)" /out/opencode; \
    baseline="$(find "$root" -type f -path "*/node_modules/opencode-linux-${arch}-baseline/bin/opencode" | head -n 1)"; \
    if [ -n "$baseline" ]; then cp "$baseline" /out/opencode-baseline; fi; \
    /out/opencode --version

# --- Stage 2: the app ---------------------------------------------------------------------------------------------
FROM python:3.12-slim

# ripgrep: OpenCode's search tool uses it (otherwise it tries to download it on first use).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=opencode /out/ /opt/opencode/
# Tiny launcher: use the "baseline" build on CPUs without AVX2 (the normal build would crash there).
RUN printf '%s\n' '#!/bin/sh' \
      'if [ -x /opt/opencode/opencode-baseline ] && ! grep -q avx2 /proc/cpuinfo 2>/dev/null; then exec /opt/opencode/opencode-baseline "$@"; fi' \
      'exec /opt/opencode/opencode "$@"' > /usr/local/bin/opencode \
    && chmod +x /usr/local/bin/opencode \
    && opencode --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY aider-model-settings.yml .

ENV PYTHONUNBUFFERED=1 \
    AIDER_CHECK_UPDATE=false \
    PORT=8000

# Render (and most PaaS) inject $PORT.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
