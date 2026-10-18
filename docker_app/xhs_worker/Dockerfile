FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS browser-download
ARG TARGETARCH
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 5 --index-url https://pypi.tuna.tsinghua.edu.cn/simple playwright==1.58.0
RUN case "$TARGETARCH" in amd64) browser_platform=debian12-x64 ;; arm64) browser_platform=debian12-arm64 ;; *) exit 1 ;; esac \
    && PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$browser_platform" PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
       playwright install chromium --no-shell

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/xhs \
    DISPLAY=:99 XHS_PRIVATE_ROOT=/var/lib/xhs PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    XHS_WORKER_TOKEN_FILE=/run/secrets/xhs_worker_token \
    HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= http_proxy= https_proxy= all_proxy=

RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify \
       libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 \
       libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \
       libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
       fonts-liberation fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*
COPY docker_app/xhs_worker/requirements.txt /tmp/xhs-requirements.txt
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 5 --index-url "$PIP_INDEX_URL" -r /tmp/xhs-requirements.txt
COPY --from=browser-download /opt/ms-playwright /opt/ms-playwright
RUN useradd --create-home --uid 1000 pwuser
WORKDIR /opt/xhs
COPY docker_app/app /opt/xhs/app
COPY docker_app/xhs_worker /opt/xhs/xhs_worker
RUN mkdir -p /var/lib/xhs && chown pwuser:pwuser /var/lib/xhs && chmod 700 /var/lib/xhs
USER pwuser
EXPOSE 8091 6080
CMD ["python", "-m", "xhs_worker.entrypoint"]
