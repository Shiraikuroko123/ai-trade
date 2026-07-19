# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.10-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY . .
RUN python -m pip wheel --wheel-dir /wheels ".[cloud]"

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="AI Trade" \
      org.opencontainers.image.description="Auditable local investment research workstation" \
      org.opencontainers.image.source="https://github.com/Shiraikuroko123/ai-trade"

ENV HOME=/tmp/ai-trade-home \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

COPY --from=builder /wheels /wheels
COPY --from=builder --chmod=0555 /build/scripts/docker-entrypoint.sh \
    /usr/local/bin/ai-trade-entrypoint
RUN python -m pip install --no-index --find-links=/wheels \
        /wheels/ai_trade-*.whl /wheels/boto3-*.whl \
    && rm -rf /wheels \
    && groupadd --gid 10001 ai-trade \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ai-trade \
    && mkdir -p /workspace/config /workspace/data/cache /workspace/reports \
        /workspace/state /workspace/logs /workspace/local \
    && chown -R 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001

EXPOSE 8765
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/login.html', timeout=3).read(1)"]

ENTRYPOINT ["ai-trade-entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--no-open", "--container-bind"]
