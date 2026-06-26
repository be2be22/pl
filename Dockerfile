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
RUN set -eux; \
    # Persist sysctl settings for host system (Railway/Docker host)
    cat >> /etc/sysctl.conf <<'EOF'

# ── Aurora v3.2 Network Tuning ──────────────────────────
# BBR congestion control (2-3x faster than Cubic on lossy links)
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr

# TCP buffer sizes (allow larger windows for high-BDP links)
net.ipv4.tcp_rmem=4096 87380 67108864
net.ipv4.tcp_wmem=4096 65536 67108864
net.core.rmem_max=67108864
net.core.wmem_max=67108864
net.core.rmem_default=262144
net.core.wmem_default=262144
net.core.netdev_max_backlog=5000

# Disable slow-start-after-idle (keep window open for keepalive connections)
net.ipv4.tcp_slow_start_after_idle=0

# Enable MTU probing (fixes black-hole MTU issues)
net.ipv4.tcp_mtu_probing=1

# TCP Fast Open (server-side, reduces 1 RTT on reconnect)
net.ipv4.tcp_fastopen=3

# Keepalive tuning (faster detection of dead connections)
net.ipv4.tcp_keepalive_time=600
net.ipv4.tcp_keepalive_intvl=15
net.ipv4.tcp_keepalive_probes=5

# Increase connection backlog (handle connection bursts)
net.core.somaxconn=8192
net.ipv4.tcp_max_syn_backlog=8192

# Disable IPv6 (avoid AAAA delays when IPv6 is broken)
net.ipv6.conf.all.disable_ipv6=1
net.ipv6.conf.default.disable_ipv6=1
# ── End Network Tuning ──────────────────────────────────
EOF
    # Try to apply immediately (will fail in unprivileged container, that's OK)
    sysctl -p 2>/dev/null || true

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

CMD ["/app/start.sh"]
