ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn

FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS browser-download
ARG TARGETARCH
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 5 --index-url https://pypi.tuna.tsinghua.edu.cn/simple playwright==1.58.0
RUN case "$TARGETARCH" in amd64) browser_platform=debian12-x64 ;; arm64) browser_platform=debian12-arm64 ;; *) exit 1 ;; esac \
    && PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$browser_platform" PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 \
       playwright install chromium --no-shell
# Keep only the Chinese/English UI resources. XHS itself is web content and is
# rendered with the bundled CJK font, so the other Chromium locale packs and the
# Widevine/hyphen/ffmpeg payloads are not needed at runtime.
RUN set -eux; \
    for browser in /opt/ms-playwright/chromium-*; do \
        [ -d "$browser" ] || continue; \
        for chrome_dir in "$browser"/chrome-linux*; do \
            [ -d "$chrome_dir" ] || continue; \
            find "$chrome_dir/locales" -maxdepth 1 -type f \
                ! -name 'en-US.pak*' ! -name 'zh-CN.pak*' -delete; \
            rm -rf "$chrome_dir/WidevineCdm" "$chrome_dir/hyphen-data"; \
        done; \
    done; \
    rm -rf /opt/ms-playwright/ffmpeg-*

# noVNC is only consumed as static core/vendor assets by browser.py. Pulling the
# Debian package in the final stage would drag in nodejs, perl, numpy and the
# OpenStack client stack (~200MB) for ~600KB of assets, so extract the .deb here
# and copy just the files we serve.
FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS novnc-assets
ARG DEBIAN_MIRROR
RUN set -eux; \
    sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    cd /tmp; \
    apt-get download novnc; \
    mkdir -p /novnc; \
    dpkg-deb -x novnc_*.deb /novnc; \
    rm -f novnc_*.deb; \
    rm -rf /var/lib/apt/lists/*

# A single classic script avoids a large ES module request graph through NAS
# remote-access proxies. Node/esbuild are build-time tools only.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS novnc-client
COPY --from=novnc-assets /novnc/usr/share/novnc /novnc
RUN npx --yes esbuild@0.25.0 /novnc/core/rfb.js --bundle --format=iife \
    --global-name=NoVNC --target=es2020 --minify --outfile=/novnc/rfb.bundle.js

FROM python:3.12-slim-bookworm
ARG DEBIAN_MIRROR

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    PYTHONPATH=/opt/zzs/docker_app:/workspace:/workspace/LivePhoto \
    APP_REPO_ROOT=/workspace \
    APP_STORAGE_ROOT=/storage \
    APP_MEDIA_WORKER_BIN=/opt/zzs/bin/media-worker \
    APP_INDEXER_BIN=/opt/zzs/bin/indexer \
    APP_THUMBNAIL_WORKERS=1 \
    XHS_EMBEDDED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Xvfb pulls in Mesa/LLVM through libgl1, but Chromium renders with its own
# ANGLE/SwiftShader stack and x11vnc does not need DRI drivers. Removing these
# in the same layer keeps the files out of the final image (~196MB).
RUN sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends xvfb x11vnc \
       libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 \
       libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \
       libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
       fonts-liberation fonts-wqy-microhei libimage-exiftool-perl \
    && find /usr/lib -maxdepth 2 -type d -name dri -exec rm -rf {} + \
    && find /usr/lib -maxdepth 2 \( -type f -o -type l \) \
             \( -name 'libLLVM-*.so*' -o -name 'libz3.so*' \
                -o -name 'libicu*.so*' -o -name 'libxml2.so*' \
                -o -name 'libtcl*.so*' -o -name 'libtk*.so*' \) -delete \
    && rm -rf /usr/share/tcltk \
    && rm -f /usr/bin/tclsh8.6 /usr/bin/wish8.6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/zzs/docker_app

COPY docker_app/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 5 -r /tmp/requirements.txt
COPY --from=browser-download /opt/ms-playwright /opt/ms-playwright
# browser.py serves only core/ and vendor/ from /usr/share/novnc.
COPY --from=novnc-assets /novnc/usr/share/novnc/core /usr/share/novnc/core
COPY --from=novnc-assets /novnc/usr/share/novnc/vendor /usr/share/novnc/vendor
COPY --from=novnc-client /novnc/rfb.bundle.js /usr/share/novnc/rfb.bundle.js
COPY --from=novnc-assets /novnc/usr/share/doc/novnc /usr/share/doc/novnc
RUN useradd --create-home --uid 1000 pwuser

RUN mkdir -p /storage/config /storage/data /workspace /workspace/LivePhoto

COPY docker_app /opt/zzs/docker_app
RUN mkdir -p /opt/zzs/bin \
    && printf '#!/bin/sh\nexec python -m app.sidecars.media_worker "$@"\n' > /opt/zzs/bin/media-worker \
    && printf '#!/bin/sh\nexec python -m app.sidecars.indexer "$@"\n' > /opt/zzs/bin/indexer \
    && chmod +x /opt/zzs/bin/media-worker /opt/zzs/bin/indexer

EXPOSE 7860

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
