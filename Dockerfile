# Two stages so the wheel-building toolchain does not ship to production.
# ffmpeg is a runtime dependency, not a build one — audio.py shells out to it for
# every voice note.

FROM python:3.11-slim AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install into a venv we can copy wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[stt,server,agent]"


FROM python:3.11-slim AS runtime

# ffmpeg + ffprobe: required at runtime by audio.py. Without them every voice
# note fails at conversion, which is a confusing way to discover a missing
# system package.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. The agent's own tools cannot write outside the
# container, but a compromised dependency should not have root either.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=json

WORKDIR /app
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser tools/ ./tools/

# Whisper caches downloaded models here. Without a writable HOME the first
# transcription fails on a read-only filesystem.
ENV HF_HOME=/home/appuser/.cache/huggingface \
    PYTHONPATH=/app/src

USER appuser
EXPOSE 8000

# Most platforms inject PORT; default for local `docker run`.
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,urllib.request; \
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\").read()"

# The app factory reads configuration at import time, so no extra wiring is
# needed. One worker on purpose — see docs/phase-6-deploy.md: conversation
# history, deduplication, and rate limiting are all per-process.
CMD ["sh", "-c", "exec uvicorn whatsapp_voice.whatsapp.app:create_app \
--factory --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1}"]
