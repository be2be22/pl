#!/bin/sh
# Entry point: bind nginx to the platform-assigned port, then launch the
# async control plane (which in turn supervises the transport core).
set -e

# Railway assigns the public port via $PORT; fall back to 8080 for local runs.
export PORT="${PORT:-8080}"
sed -i "s/PORT_PLACEHOLDER/$PORT/" /etc/nginx/nginx.conf

mkdir -p /tmp
echo "[boot] edge router -> :$PORT"

# v3.2: Apply network tuning at runtime (best-effort; needs privileged mode).
# These settings persist in /etc/sysctl.conf from Dockerfile, but container
# runtime may not have applied them. We try and silently ignore failures.
if command -v sysctl >/dev/null 2>&1; then
    sysctl -w net.core.default_qdisc=fq 2>/dev/null || true
    sysctl -w net.ipv4.tcp_congestion_control=bbr 2>/dev/null || true
    sysctl -w net.ipv4.tcp_fastopen=3 2>/dev/null || true
    sysctl -w net.ipv4.tcp_slow_start_after_idle=0 2>/dev/null || true
    sysctl -w net.ipv4.tcp_mtu_probing=1 2>/dev/null || true
    sysctl -w net.ipv4.tcp_rmem="4096 87380 67108864" 2>/dev/null || true
    sysctl -w net.ipv4.tcp_wmem="4096 65536 67108864" 2>/dev/null || true
    sysctl -w net.core.rmem_max=67108864 2>/dev/null || true
    sysctl -w net.core.wmem_max=67108864 2>/dev/null || true
    sysctl -w net.core.somaxconn=8192 2>/dev/null || true
    echo "[boot] network tuning applied (or skipped if unprivileged)"
fi

# Validate the (substituted) nginx config up front.
# On failure, print the full config + error so debugging is possible from logs.
if ! nginx -t 2>&1; then
    echo "[boot] FATAL: nginx config test failed. Dumping config:"
    echo "------ nginx.conf ------"
    cat /etc/nginx/nginx.conf
    echo "------ end config ------"
    exit 1
fi

# Launch nginx as a background daemon.
nginx

# tiny settle so the listener is up before the core registers
sleep 1

echo "[boot] control plane starting (v3.2)"
exec python3 /app/main.py
