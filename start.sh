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
# Settings are loaded from /etc/sysctl.d/99-aurora.conf (copied in Dockerfile).
# Railway containers may not allow sysctl changes — we silently ignore failures.
if command -v sysctl >/dev/null 2>&1; then
    # Try loading from our config file first
    if [ -f /etc/sysctl.d/99-aurora.conf ]; then
        sysctl -p /etc/sysctl.d/99-aurora.conf 2>/dev/null || true
    fi
    # Critical settings — try individually (some may succeed even if others fail)
    sysctl -w net.ipv4.tcp_congestion_control=bbr 2>/dev/null || true
    sysctl -w net.ipv4.tcp_fastopen=3 2>/dev/null || true
    sysctl -w net.ipv4.tcp_slow_start_after_idle=0 2>/dev/null || true
    sysctl -w net.ipv4.tcp_mtu_probing=1 2>/dev/null || true
    # v3.5: Ultra-low buffers for 100+ concurrent users (4MB max per connection)
    sysctl -w net.ipv4.tcp_rmem="4096 32768 4194304" 2>/dev/null || true
    sysctl -w net.ipv4.tcp_wmem="4096 32768 4194304" 2>/dev/null || true
    sysctl -w net.core.rmem_max=4194304 2>/dev/null || true
    sysctl -w net.core.wmem_max=4194304 2>/dev/null || true
    sysctl -w net.core.somaxconn=1024 2>/dev/null || true
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
