# Development-only isolated acceptance image, not a public runtime/release package.
# This digest is the inspected ARM64 image. Do not label it an x64 validation.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/context-browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg openssl fonts-wqy-zenhei libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY collection_context-0.2.0.dev0-py3-none-any.whl /opt/candidate.whl
RUN mv /opt/candidate.whl /opt/collection_context-0.2.0.dev0-py3-none-any.whl \
    && python -m pip install --no-cache-dir \
       '/opt/collection_context-0.2.0.dev0-py3-none-any.whl[mcp,web,browser,ocr]' \
       pytest==9.1.1 Pillow==12.3.0 \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/context-browsers
COPY backend /acceptance/backend
COPY frontend /acceptance/frontend
WORKDIR /acceptance
USER 10001:10001
CMD ["python", "/acceptance/backend/tools/linux_acceptance/runner.py", "--output", "/results", "--models", "/models", "--expected-machine", "aarch64"]
