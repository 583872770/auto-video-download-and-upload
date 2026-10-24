# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS javascript

FROM python:3.13-slim-bookworm
ARG WITH_YT5S_BROWSER=0
ARG DEBIAN_MIRROR=https://mirrors.aliyun.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    YT5S_BROWSER_ENABLED=${WITH_YT5S_BROWSER}
WORKDIR /app

# The Bookworm base image uses the deb822 sources file.
RUN sed -i \
      -e "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" \
      -e "s|https://deb.debian.org|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates ffmpeg tzdata \
    && if [ "$WITH_YT5S_BROWSER" = "1" ]; then \
         DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
           chromium xvfb x11vnc novnc websockify fonts-noto-cjk; \
       fi \
    && rm -rf /var/lib/apt/lists/*
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.lock.txt

COPY docker/install_mihomo.py /tmp/install_mihomo.py
ARG MIHOMO_DOWNLOAD_BASE_URL=""
RUN python /tmp/install_mihomo.py \
    && rm /tmp/install_mihomo.py \
    && node --version && ffmpeg -version && ffprobe -version

COPY app.py web_worker.py ./
COPY Utils/ ./Utils/
COPY webapp/ ./webapp/
COPY templates/ ./templates/
COPY static/ ./static/
COPY scripts/login_youtube.py ./scripts/login_youtube.py
COPY scripts/migrate_mysql.py ./scripts/migrate_mysql.py
COPY docker/ ./docker/
RUN mkdir -p /data/config /data/videos /app/.cache/web

EXPOSE 5000
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["python", "-u", "/app/app.py", "--host", "0.0.0.0", "--port", "5000", "--config", "/data/config/config.yaml"]
