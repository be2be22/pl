#!/bin/sh
# Entry point: bind nginx to the platform-assigned port, then launch the
# async control plane (which in turn supervises the transport core).
set -e

# Railway assigns the public port via $PORT; fall back to 8080 for local runs.
export PORT="${PORT:-8080}"
sed -i "s/PORT_PLACEHOLDER/$PORT/" /etc/nginx/nginx.conf

mkdir -p /tmp
echo "[boot] edge router -> :$PORT"

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

echo "[boot] control plane starting (v3.1)"
exec python3 /app/main.py
