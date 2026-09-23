# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="wsi-patchkit" \
      org.opencontainers.image.description="Whole-slide image viewer and patch service"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 patchkit \
    && useradd --system --uid 10001 --gid patchkit --home-dir /app patchkit \
    && mkdir -p /data/slides /data/overlays /data/crops /data/uploads \
    && chown -R patchkit:patchkit /app /data

# Copy packaging metadata first so dependency installation remains cached when
# only application source files change.
COPY --chown=patchkit:patchkit pyproject.toml README.md LICENSE ./
COPY --chown=patchkit:patchkit src ./src

RUN python -m pip install --no-cache-dir ".[web]" -i https://pypi.tuna.tsinghua.edu.cn/simple

USER patchkit

EXPOSE 8000
VOLUME ["/data/crops", "/data/uploads"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/slides', timeout=3)"]

ENTRYPOINT ["wsi-patchkit-viewer"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--slide-dir", "/data/slides", "--overlay-root", "/data/overlays", "--crop-output-dir", "/data/crops", "--upload-dir", "/data/uploads"]
