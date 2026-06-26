FROM python:3.11-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    CORE_VER=v25.3.6 XRAY_LOCATION_ASSET=/usr/local/bin
WORKDIR /app

# System deps + Xray (pinned version) + GeoIP DB
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates nginx procps && \
    rm -rf /var/lib/apt/lists/*

# v3.2: Network performance tuning — BBR + TCP buffers
# These sysctl settings dramatically improve throughput on high-latency
# or lossy links (mobile networks, Iran).
# Settings are written to /etc/sysctl.d/ for host persistence.
# Runtime application happens in start.sh (best-effort, may fail in unprivileged mode).
COPY sysctl-aurora.conf /etc/sysctl.d/99-aurora.conf

RUN set -eux; \
    curl -fsSL -o /tmp/c.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${CORE_VER}/Xray-linux-64.zip"; \
    unzip -q /tmp/c.zip -d /tmp/core; \
    mv /tmp/core/xray /usr/local/bin/core; \
    chmod +x /usr/local/bin/core; \
    mv /tmp/core/geoip.dat /usr/local/bin/geoip.dat 2>/dev/null || true; \
    mv /tmp/core/geosite.dat /usr/local/bin/geosite.dat 2>/dev/null || true; \
    rm -rf /tmp/c.zip /tmp/core

RUN set -eux; \
    DB_MONTH=$(date +%Y-%m); \
    curl -fsSL -o /tmp/dbip.mmdb.gz \
      "https://download.db-ip.com/free/dbip-country-lite-${DB_MONTH}.mmdb.gz" && \
    gzip -d /tmp/dbip.mmdb.gz && \
    mkdir -p /app/data && \
    mv /tmp/dbip.mmdb /app/data/dbip-country-lite.mmdb || \
    echo "GeoIP DB download failed, will run without country lookup"

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app /app/app
COPY main.py /app/main.py
COPY start.sh /app/start.sh
COPY nginx.conf /etc/nginx/nginx.conf

RUN chmod +x /app/start.sh

EXPOSE 8080
# v3.3: Extra WS ports (default 2083; configurable via EXTRA_WS_PORTS env)
EXPOSE 2083 2053 2087

CMD ["/app/start.sh"]
