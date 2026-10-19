# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

LABEL org.opencontainers.image.title="wsi-patchkit" \
      org.opencontainers.image.description="Whole-slide image viewer and patch service"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 patchkit \
    && useradd --system --uid 10001 --gid patchkit --home-dir /app patchkit \
    && mkdir -p /data/slides /data/overlays /data/crops /data/uploads /data/cache \
    && chown -R patchkit:patchkit /app /data

# Copy packaging metadata first so dependency installation remains cached when
# only application source files change.
COPY --chown=patchkit:patchkit pyproject.toml README.md LICENSE ./
COPY --chown=patchkit:patchkit src ./src

RUN uv venv /app/.venv && uv pip install --python /app/.venv/bin/python ".[web]"

USER patchkit

EXPOSE 8000
VOLUME ["/data/crops", "/data/uploads", "/data/cache"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

ENTRYPOINT ["wsi-patchkit-viewer"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--slide-dir", "/data/slides", "--overlay-root", "/data/overlays", "--crop-output-dir", "/data/crops", "--upload-dir", "/data/uploads", "--kfb-index-cache-dir", "/data/cache/kfb-index"]
