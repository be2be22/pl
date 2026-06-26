"""Xray-core subprocess management with thread-safe access.

v3 changes:
  - `_proc_lock` (threading.Lock) guards all access to the global _proc.
  - Exponential backoff on repeated resync failures (avoids tight crash-loop).
  - Spawn-wait uses polling (0.1s) instead of blocking sleep(1.5).
  - Stderr reader only logs lines containing error/failed (filters INFO noise).
  - `alive()` is lock-protected and safe to call from any thread/coroutine.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import threading
import time
from collections import deque

from . import config, state

_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_resync_needed: bool = False
_resync_failures: int = 0
# v3.5: Reduced from 500 to 100 to save RAM (each line ~200 bytes)
_log_queue: deque = deque(maxlen=100)


def get_logs() -> list[str]:
    """Drain the log queue (called by accounting loop for Axiom shipping)."""
    logs: list[str] = []
    while _log_queue:
        logs.append(_log_queue.popleft())
    return logs


def _read_stderr(proc: subprocess.Popen) -> None:
    """Background thread: read stderr lines, queue only errors/warnings.

    v3.3: with loglevel=warning, stderr volume is much lower. But we still
    filter to only queue lines that contain error/failed/panic to keep
    _log_queue (and Axiom shipping) lean.
    """
    try:
        if proc.stderr:
            for line in proc.stderr:
                clean = line.strip()
                if not clean:
                    continue
                low = clean.lower()
                # Only queue lines that are actual errors (skip warnings/info)
                if "error" in low or "failed" in low or "panic" in low:
                    _log_queue.append(clean)
                    state.log_error(f"XRAY: {clean[:200]}")
    except Exception:
        pass


def _ensure_keys_sync() -> None:
    """Generate Reality x25519 keys if missing."""
    r = state.STATS["reality"]
    if r.get("priv") and r.get("pub") and r.get("sid"):
        return
    try:
        out = subprocess.run(
            [config.CORE_BIN, "x25519"],
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout
        priv = pub = ""
        for m in ("Private key:", "PrivateKey:"):
            if m in out:
                priv = out.split(m)[1].splitlines()[0].strip()
                break
        for m in (
            "Password (PublicKey):",
            "Public key:",
            "PublicKey:",
            "Password:",
        ):
            if m in out:
                pub = out.split(m)[1].splitlines()[0].strip()
                break
        if priv and pub:
            r["priv"], r["pub"] = priv, pub
            r["sid"] = secrets.token_hex(4)
            state.mark_dirty()
    except Exception as e:
        state.log_error(f"keygen: {e}")


async def ensure_keys() -> None:
    await asyncio.to_thread(_ensure_keys_sync)


def _client(uid: str, tag: str) -> dict:
    c = {"id": uid, "email": uid, "level": 0}
    if tag == "reality":
        c["flow"] = "xtls-rprx-vision"
    return c


def _active(u: dict, uid: str) -> bool:
    if u.get("status") in ("disabled", "expired"):
        return False
    if u.get("expiry") and time.time() > u["expiry"]:
        return False
    if u.get("quota") and state.user_used(uid) >= u["quota"]:
        return False
    return True


def _ws_inbound(clients: list) -> dict:
    return {
        "tag": "ws",
        "listen": "127.0.0.1",
        "port": config.WS_PORT,
        "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": {
            "network": "ws",
            "security": "none",
            "wsSettings": {"path": config.WS_PATH},
        },
    }


def _grpc_inbound(clients: list) -> dict:
    """VLESS over gRPC transport.

    gRPC multiplexes many streams over a single HTTP/2 connection, which:
      - Eliminates per-message WebSocket framing overhead
      - Recovers from packet loss faster (independent stream windows)
      - Better suits mobile networks with intermittent connectivity
      - Lower latency for small frequent packets (DNS, etc.)
    """
    return {
        "tag": "grpc",
        "listen": "127.0.0.1",
        "port": config.GRPC_PORT,
        "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": {
            "network": "grpc",
            "security": "none",
            "grpcSettings": {"serviceName": config.GRPC_PATH.lstrip("/")},
        },
    }


def _reality_inbound(clients: list) -> dict:
    r = state.STATS["reality"]
    return {
        "tag": "reality",
        "listen": "0.0.0.0",
        "port": config.REALITY_APP_PORT,
        "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": config.REALITY_DEST,
                "xver": 0,
                "serverNames": config.reality_servernames(),
                "privateKey": r["priv"],
                "shortIds": [r["sid"]],
            },
        },
    }


def _api_inbound() -> dict:
    return {
        "tag": "api",
        "listen": "127.0.0.1",
        "port": config.API_PORT,
        "protocol": "dokodemo-door",
        "settings": {"address": "127.0.0.1"},
    }


def build_config() -> dict:
    ws_clients: list = []
    grpc_clients: list = []
    re_clients: list = []
    with state.lock:
        for uid, u in state.USERS.items():
            if not _active(u, uid):
                continue
            protos = u.get("protocols", ["ws", "grpc", "reality"])
            if "ws" in protos:
                ws_clients.append(_client(uid, "ws"))
            if "grpc" in protos:
                grpc_clients.append(_client(uid, "grpc"))
            if "reality" in protos:
                re_clients.append(_client(uid, "reality"))

    # v3.2: shared sockopt for inbounds — TCP Fast Open + NoDelay
    # TFO saves 1 RTT on new connections; NoDelay disables Nagle's algorithm
    # (reduces latency for small packets like DNS queries).
    # NOTE: tcpKeepAliveInterval/Timeout may not be supported in older Xray;
    #       only enable the most compatible options here.
    inbound_sockopt = {
        "tcpFastOpen": True,
        "tcpNoDelay": True,
    }

    inbounds = [
        _api_inbound(),
        _ws_inbound(ws_clients),
    ]
    # Only include gRPC inbound if there are clients (avoids empty listener)
    if grpc_clients:
        inbounds.append(_grpc_inbound(grpc_clients))
    inbounds.append(_reality_inbound(re_clients))

    # Apply sockopt to all transport inbounds (skip api inbound)
    for ib in inbounds:
        tag = ib.get("tag", "")
        if tag in ("ws", "grpc", "reality"):
            ss = ib.setdefault("streamSettings", {})
            ss.setdefault("sockopt", {}).update(inbound_sockopt)

    return {
        "log": {
            # v3.3: loglevel=warning (was info) — info floods stderr with
            # connection logs that consume CPU/RAM via _read_stderr thread.
            # access log (separate file) still captures accepted IPs.
            "loglevel": "warning",
            "access": config.CORE_CFG_ACCESS_LOG,
            "dnsLog": False,
        },
        # v3.4: DNS — simplified to plain UDP (was DoH which consumed ~50MB RAM)
        # DoH client in Xray creates an internal HTTP/2 connection pool that
        # significantly increases memory usage on small containers.
        # Plain UDP to 1.1.1.1 is nearly as fast (cached after first query)
        # and uses minimal RAM. Xray's built-in cache handles repeated lookups.
        "dns": {
            "servers": [
                "1.1.1.1",
                "8.8.8.8",
            ],
            "queryStrategy": "UseIPv4",
            "disableCache": False,
            "disableFallback": False,
        },
        "stats": {},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "handshake": 4,        # Fail fast on bad clients
                    # v3.6: Critical RAM savers for speed-test scenarios
                    # bufferSize: Xray's internal per-connection buffer (MB).
                    #   2MB supports ~50Mbps with 300ms RTT (covers most Iran users)
                    #   without causing RAM spikes during speed tests.
                    "bufferSize": 2,
                    # connIdle: close idle connections after 60s (not too aggressive)
                    "connIdle": 60,
                    # uplinkOnly/downlinkOnly: half-closed connection timeout.
                    "uplinkOnly": 15,
                    "downlinkOnly": 15,
                }
            },
            "system": {
                "statsOutboundUplink": False,
                "statsOutboundDownlink": False,
                "statsInboundUplink": False,
                "statsInboundDownlink": False,
            },
        },
        "inbounds": inbounds,
        "outbounds": [
            # v3.2: freedom outbound with IPv4-only strategy
            # NOTE: streamSettings/sockopt removed from freedom outbound
            #       because it can cause config validation errors on some Xray builds.
            #       The inbound sockopt is sufficient for TFO benefits.
            {
                "protocol": "freedom",
                "tag": "direct",
                "settings": {
                    "domainStrategy": "UseIPv4",  # Skip AAAA resolution
                },
            },
            {"protocol": "blackhole", "tag": "block"},
            {"protocol": "blackhole", "tag": "api"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                # Route API traffic to blackhole (already standard)
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                # v3.2: Block ads/malware at routing level (saves bandwidth)
                {
                    "type": "field",
                    "domain": ["geosite:category-ads-all"],
                    "outboundTag": "block",
                },
                # v3.2: Private network = direct (LAN access)
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
            ],
        },
    }


def _spawn() -> bool:
    """Spawn Xray subprocess. Returns True if it stays alive past initial wait."""
    global _proc
    try:
        cfg = build_config()
        with open(config.CORE_CFG, "w") as f:
            json.dump(cfg, f, separators=(",", ":"))

        # Ensure access log directory exists (Xray won't create it)
        import os
        access_log_dir = os.path.dirname(config.CORE_CFG_ACCESS_LOG)
        if access_log_dir:
            os.makedirs(access_log_dir, exist_ok=True)

        with _proc_lock:
            _proc = subprocess.Popen(
                [config.CORE_BIN, "run", "-c", config.CORE_CFG],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

        # Poll for early crash (avoids 1.5s blocking sleep)
        deadline = time.time() + config.ENGINE_SPAWN_WAIT
        while time.time() < deadline:
            with _proc_lock:
                p = _proc
            if p and p.poll() is not None:
                state.log_error(
                    f"Xray exited immediately with code {p.returncode}. "
                    "Config might be invalid."
                )
                return False
            time.sleep(0.1)

        with _proc_lock:
            p = _proc
        if p and p.poll() is None:
            t = threading.Thread(
                target=_read_stderr, args=(p,), daemon=True
            )
            t.start()
            return True
        return False
    except Exception as e:
        state.log_error(f"core spawn: {e}")
        return False


def _get_backoff() -> float:
    """Exponential backoff: 2, 4, 8, 16, 32, 60, 60, ..."""
    global _resync_failures
    if _resync_failures <= 0:
        return 0.0
    import math
    secs = min(
        config.ENGINE_RESYNC_BACKOFF_BASE * (2 ** (_resync_failures - 1)),
        config.ENGINE_RESYNC_BACKOFF_MAX,
    )
    return secs


def resync() -> None:
    """Restart Xray with current config. Thread-safe via _proc_lock."""
    global _proc, _resync_needed, _resync_failures
    try:
        _ensure_keys_sync()
        with _proc_lock:
            old = _proc
        if old and old.poll() is None:
            old.terminate()
            try:
                old.wait(timeout=4)
            except Exception:
                old.kill()
                try:
                    old.wait(timeout=2)
                except Exception:
                    pass
        ok = _spawn()
        with _proc_lock:
            _resync_needed = not ok
        if ok:
            _resync_failures = 0
        else:
            _resync_failures += 1
            backoff = _get_backoff()
            if backoff > 0:
                state.log_error(
                    f"core resync failed ({_resync_failures}x); "
                    f"backing off {backoff:.0f}s"
                )
                time.sleep(backoff)
    except Exception as e:
        state.log_error(f"core resync: {e}")
        with _proc_lock:
            _resync_needed = True


def alive() -> bool:
    """Thread-safe liveness check."""
    with _proc_lock:
        return bool(_proc and _proc.poll() is None)


def should_backoff() -> bool:
    """True if resync should be delayed due to recent failures."""
    return _get_backoff() > 0


async def _api(*args: str, timeout: int = 5) -> tuple[int, str]:
    """Call Xray API via subprocess. Returns (returncode, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            config.CORE_BIN,
            "api",
            args[0],
            f"--server=127.0.0.1:{config.API_PORT}",
            *args[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", "ignore")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, ""
    except Exception as e:
        state.log_error(f"core api {args[0]}: {e}")
        return -1, ""


async def hot_add(uid: str) -> bool:
    """Add user to running Xray via API. Returns True if all protocols OK."""
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return False
        protos = list(u.get("protocols", ["ws", "grpc", "reality"]))
    ok = True
    for tag in protos:
        args = ["adu", f"-tag={tag}", f"-id={uid}", f"-email={uid}"]
        if tag == "reality":
            args.append("-flow=xtls-rprx-vision")
        rc, _ = await _api(*args)
        if rc != 0:
            ok = False
    return ok


async def hot_remove(uid: str) -> None:
    """Remove user from running Xray via API. Ignores 'not found' (rc=2)."""
    for tag in ("ws", "grpc", "reality"):
        await _api("rmu", f"-tag={tag}", f"-email={uid}", timeout=4)


async def apply_user(uid: str) -> None:
    """Remove-then-re-add user (atomic update for protocol/quota changes)."""
    with state.lock:
        u = state.USERS.get(uid)
        u_copy = dict(u) if u else None
    await hot_remove(uid)
    if u_copy and _active(u_copy, uid):
        ok = await hot_add(uid)
        if not ok:
            mark_resync()


async def query_stats() -> dict:
    """Query & reset stats counters from Xray API."""
    rc, out = await _api("statsquery", "-reset")
    if rc != 0 or not out:
        return {}
    try:
        return json.loads(out)
    except Exception:
        return {}


def mark_resync() -> None:
    global _resync_needed
    _resync_needed = True


def resync_pending() -> bool:
    return _resync_needed


def reset_backoff() -> None:
    global _resync_failures
    _resync_failures = 0
