"""Background accounting loop: stats polling, IP tracking, alerts, cleanup.

v3 changes:
  - _read_log_tail now reads from END of file (not start) + bounded by bytes.
    Previously it read the first N lines, losing the newest entries entirely.
  - sysmetrics.refresh() runs in a worker thread (no event-loop blocking).
  - Axiom sends are fire-and-forget tasks (don't block the accounting loop).
  - IP eviction policy when MAX_TRACKED_IPS reached (oldest, not silent skip).
  - History uses deque(maxlen=...) (O(1) append, auto-evict).
  - Cleanup loop also evicts stale IP_STATS entries (not just ACTIVE_IPS).
  - Exponential backoff integration with engine.should_backoff().
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from . import config, state, engine, sysmetrics, axiom_logs

_ACCEPTED_RE = re.compile(r" (\d+\.\d+\.\d+\.\d+):\d+ accepted ")
_PROXY_LINE_RE = re.compile(r"^([^|]*)\|(.+?)\s+(/\S+)$")
_PROXY_LINE_OLD = re.compile(r"^(\S+)\s+(/\S+)$")
_proxy_read_pos: int = 0

import ipaddress as _ipaddr
_CF_NETS = tuple(
    _ipaddr.ip_network(n, strict=False)
    for n in (
        "104.16.0.0/12", "108.162.192.0/18", "131.0.72.0/22",
        "141.101.64.0/18", "162.158.0.0/15", "172.64.0.0/13",
        "173.245.48.0/20", "188.114.96.0/20", "190.93.240.0/20",
        "197.234.240.0/22", "198.41.128.0/17",
        "89.222.0.0/15", "172.70.0.0/15", "172.68.0.0/14",
    )
)


def _is_cloudflare_ip(ip: str) -> bool:
    try:
        addr = _ipaddr.ip_address(ip)
        return any(addr in net for net in _CF_NETS)
    except ValueError:
        return False


_PROXY_INTERNAL = (
    "100.64.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    "192.168.", "127.", "152.233.", "20.20.", "20.40.", "20.190.",
    "20.140.", "40.90.", "40.126.", "52.96.", "52.112.",
)


def _real_ip(raw: str) -> str | None:
    """Extract real client IP from X-Forwarded-For chain, skipping proxies.
    Chain order is: nearest_proxy, ..., original_client (last = real IP)."""
    parts = [p.strip() for p in raw.split(",")]
    for part in reversed(parts):
        ip = part.strip()
        if not ip or ip == "-":
            continue
        if _is_cloudflare_ip(ip):
            continue
        if any(ip.startswith(p) for p in _PROXY_INTERNAL):
            continue
        return ip
    return None


def _read_log_tail(filepath: str, max_bytes: int, truncate: bool = True) -> list[str]:
    """Read the TAIL of the access log (newest entries)."""
    if not os.path.exists(filepath):
        return []
    try:
        size = os.path.getsize(filepath)
        if size == 0:
            return []
        read_bytes = min(max_bytes, 131072)
        with open(filepath, "rb") as f:
            if size > read_bytes:
                f.seek(-read_bytes, 2)
                f.readline()
            raw = f.read()
        if truncate:
            with open(filepath, "w"):
                pass
        elif size > 2097152:
            with open(filepath, "w"):
                pass
        text = raw.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        if len(lines) > 500:
            lines = lines[-500:]
        return lines
    except Exception:
        return []


def _extract_client_ip(line: str) -> str | None:
    m = _ACCEPTED_RE.search(line)
    if not m:
        return None
    ip = m.group(1)
    if ip == "127.0.0.1" or ip.startswith("100.64.") or ip == "-":
        return None
    if _is_cloudflare_ip(ip):
        return None
    return ip


def _evict_oldest_ip() -> None:
    """Evict the IP with the oldest 'last' timestamp when table is full."""
    if not state.IP_STATS:
        return
    oldest_ip = None
    oldest_ts = float("inf")
    for ip, rec in state.IP_STATS.items():
        t = rec.get("last", 0)
        if t < oldest_ts:
            oldest_ts = t
            oldest_ip = ip
    if oldest_ip:
        state.IP_STATS.pop(oldest_ip, None)
        state.ACTIVE_IPS.discard(oldest_ip)


def _parse_realtime_ips() -> None:
    """Parse access logs (Xray + nginx proxy), update IP_STATS and ACTIVE_IPS."""
    now = time.time()
    seen: dict[str, int] = {}

    # Xray access log (direct connections, Reality)
    for line in _read_log_tail(
        config.CORE_CFG_ACCESS_LOG, config.IP_LOG_MAX_BYTES
    ):
        ip = _extract_client_ip(line)
        if ip:
            seen[ip] = seen.get(ip, 0) + 1

    # Nginx proxy log — incremental read (only new lines since last read)
    proto_seen: dict[str, dict[str, int]] = {}
    global _proxy_read_pos
    try:
        path = config.PROXY_ACCESS_LOG
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > _proxy_read_pos:
                with open(path, "rb") as f:
                    f.seek(_proxy_read_pos)
                    raw = f.read()
                _proxy_read_pos = f.tell()
                for line in raw.decode("utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    m = _PROXY_LINE_RE.match(line)
                    if m:
                        cf_ip, xff_raw, uri = m.group(1).strip(), m.group(2), m.group(3)
                        ip = _real_ip(cf_ip) if cf_ip else _real_ip(xff_raw)
                        if not ip:
                            continue
                        seen[ip] = seen.get(ip, 0) + 1
                        proto = "ws" if "/ws" in uri else "grpc" if "/grpc" in uri else ""
                        if proto:
                            proto_seen.setdefault(ip, {})[proto] = (
                                proto_seen.get(ip, {}).get(proto, 0) + 1
                            )
                        continue
                    m = _PROXY_LINE_OLD.match(line)
                    if m:
                        ip = _real_ip(m.group(1))
                        uri = m.group(2)
                        if not ip:
                            continue
                        seen[ip] = seen.get(ip, 0) + 1
                        proto = "ws" if "/ws" in uri else "grpc" if "/grpc" in uri else ""
                        if proto:
                            proto_seen.setdefault(ip, {})[proto] = (
                                proto_seen.get(ip, {}).get(proto, 0) + 1
                            )
            elif size < _proxy_read_pos:
                _proxy_read_pos = 0
            elif size > 2097152:
                with open(path, "w") as f:
                    pass
                _proxy_read_pos = 0
    except Exception:
        pass

    if not seen:
        return

    with state.lock:
        for ip, hits in seen.items():
            rec = state.IP_STATS.get(ip)
            if not rec:
                # Eviction policy: drop oldest instead of silent skip
                if len(state.IP_STATS) >= config.MAX_TRACKED_IPS:
                    _evict_oldest_ip()
                rec = {
                    "up": 0,
                    "down": 0,
                    "up_delta": 0,
                    "down_delta": 0,
                    "conns": 0,
                    "last": now,
                    "first_seen": now,
                }
                state.IP_STATS[ip] = rec
            rec["last"] = now
            rec["conns"] = rec.get("conns", 0) + hits
            ip_proto = proto_seen.get(ip)
            if ip_proto:
                rec["proto"] = ip_proto
            state.ACTIVE_IPS.add(ip)


async def _ip_tracker_loop() -> None:
    """Fast loop (2s) for real-time IP tracking + immediate RAM cleanup."""
    while True:
        try:
            await asyncio.to_thread(_parse_realtime_ips)
            now = time.time()
            with state.lock:
                # Remove stale IPs AND all Cloudflare IPs
                stale = [
                    ip
                    for ip in list(state.ACTIVE_IPS)
                    if now - state.IP_STATS.get(ip, {}).get("last", 0)
                    > config.ONLINE_WINDOW
                    or _is_cloudflare_ip(ip)
                ]
                for ip in stale:
                    state.ACTIVE_IPS.discard(ip)
                    state.IP_STATS.pop(ip, None)
        except Exception as e:
            state.log_error(f"ip_tracker: {e}")
        await asyncio.sleep(config.IP_TRACKER_INTERVAL)


async def _enforce_time_limits() -> bool:
    """Expire users who hit time/quota limits. Returns True if any changed."""
    now = time.time()
    to_revoke: list[str] = []
    with state.lock:
        for uid, u in state.USERS.items():
            if u.get("status") in ("disabled", "expired"):
                continue
            over_time = u.get("expiry") and now > u["expiry"]
            over_quota = (
                u.get("quota") and state.user_used(uid) >= u["quota"]
            )
            if over_time or over_quota:
                u["status"] = "expired"
                to_revoke.append(uid)
    for uid in to_revoke:
        await engine.hot_remove(uid)
    return bool(to_revoke)


async def _cleanup_task() -> None:
    """Periodic RAM hygiene: stale LAST_ACTIVE, orphan stats, dead alerts.

    Wrapped in try/except so a single exception doesn't kill the loop forever.
    """
    while True:
        await asyncio.sleep(config.CLEANUP_INTERVAL)
        try:
            now = time.time()
            with state.lock:
                # Stale LAST_ACTIVE entries (>5min inactive)
                stale_users = [
                    uid for uid, t in state.LAST_ACTIVE.items() if now - t > 300
                ]
                for uid in stale_users:
                    state.LAST_ACTIVE.pop(uid, None)
                # Alert keys for deleted users
                stale_alerts = [
                    k
                    for k in state.ALERTS_SENT
                    if ":" in k and k.split(":", 1)[1] not in state.USERS
                ]
                for k in stale_alerts:
                    state.ALERTS_SENT.discard(k)
                # Orphan stats entries
                orphan = [uid for uid in state.STATS["users"] if uid not in state.USERS]
                for uid in orphan:
                    state.STATS["users"].pop(uid, None)
                    state.LAST_ACTIVE.pop(uid, None)
                # Evict stale IP_STATS entries AND Cloudflare IPs
                stale_ips = [
                    ip
                    for ip, rec in state.IP_STATS.items()
                    if now - rec.get("last", 0) > config.ONLINE_WINDOW * 2
                    or _is_cloudflare_ip(ip)
                ]
                for ip in stale_ips:
                    state.IP_STATS.pop(ip, None)
                    state.ACTIVE_IPS.discard(ip)
        except Exception as e:
            state.log_error(f"cleanup_task: {e}")


async def _alerts_check() -> None:
    """Admin Telegram alerts with anti-spam (dedup via ALERTS_SENT)."""
    from . import bot as _bot

    if not _bot.enabled():
        return
    now = time.time()
    msgs: list[str] = []

    alive = engine.alive()
    if alive != state.LAST_CORE_ALIVE:
        msgs.append(
            "✅ هسته دوباره فعال شد."
            if alive
            else "⚠️ هسته از کار افتاد (در حال تلاش برای بازیابی)."
        )
        state.LAST_CORE_ALIVE = alive

    sys = state.SYS
    mem_pct = (sys["mem_used"] / sys["mem_total"] * 100) if sys["mem_total"] else 0
    if mem_pct >= config.ALERT_MEM_PCT:
        if "mem_high" not in state.ALERTS_SENT:
            msgs.append(f"🧠 هشدار: مصرف حافظه به {mem_pct:.0f}٪ رسید.")
            state.ALERTS_SENT.add("mem_high")
    else:
        state.ALERTS_SENT.discard("mem_high")

    if sys["cpu"] >= config.ALERT_CPU_PCT:
        if "cpu_high" not in state.ALERTS_SENT:
            msgs.append(f"⚡️ هشدار: مصرف CPU به {sys['cpu']:.0f}٪ رسید.")
            state.ALERTS_SENT.add("cpu_high")
    else:
        state.ALERTS_SENT.discard("cpu_high")

    with state.lock:
        users = list(state.USERS.items())
        usage_snapshot = {uid: state.user_used(uid) for uid, _ in users}

    for uid, u in users:
        if u.get("status") != "active":
            continue
        label = u.get("label", uid[:8])
        quota = u.get("quota", 0)
        if quota:
            used = usage_snapshot.get(uid, 0)
            pct = used / quota * 100
            if pct >= 95 and f"q95:{uid}" not in state.ALERTS_SENT:
                msgs.append(f"🔴 «{label}» به {pct:.0f}٪ حجم رسید.")
                state.ALERTS_SENT.add(f"q95:{uid}")
            elif 80 <= pct < 95 and f"q80:{uid}" not in state.ALERTS_SENT:
                msgs.append(f"🟡 «{label}» به {pct:.0f}٪ حجم رسید.")
                state.ALERTS_SENT.add(f"q80:{uid}")
        expiry = u.get("expiry", 0)
        if expiry:
            days_left = (expiry - now) / 86400
            if (
                0 < days_left <= config.ALERT_EXPIRY_DAYS
                and f"exp:{uid}" not in state.ALERTS_SENT
            ):
                msgs.append(
                    f"⏳ «{label}» تا {days_left:.1f} روز دیگر منقضی می‌شود."
                )
                state.ALERTS_SENT.add(f"exp:{uid}")

    for m in msgs:
        await _bot.notify_admin(m)


async def _safe_send_to_axiom(payload: list, event_type: str) -> None:
    """Fire-and-forget Axiom send; errors are logged, never propagated."""
    try:
        await axiom_logs.send_to_axiom(payload, event_type)
    except Exception as e:
        state.log_error(f"axiom send ({event_type}): {e}")


# Tracked background tasks (cancelled on shutdown via cancel_all())
_bg_tasks: set = set()


def _spawn(coro) -> None:
    """Spawn a tracked background task (cancelable via cancel_all)."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def cancel_all() -> None:
    """Cancel all accounting background tasks (called on shutdown)."""
    for t in list(_bg_tasks):
        t.cancel()
    if _bg_tasks:
        await asyncio.gather(*list(_bg_tasks), return_exceptions=True)


async def loop() -> None:
    """Main accounting loop: runs forever after a 4s startup grace period."""
    _spawn(_cleanup_task())
    _spawn(_ip_tracker_loop())

    await asyncio.sleep(4)

    while True:
        try:
            # sysmetrics in worker thread (filesystem I/O, don't block loop)
            await asyncio.to_thread(sysmetrics.refresh)

            if engine.should_backoff():
                pass
            elif not engine.alive() or engine.resync_pending():
                await asyncio.to_thread(engine.resync)
                engine.reset_backoff()
                if not engine.alive():
                    backoff = engine.get_backoff_secs()
                    if backoff > 0:
                        await asyncio.sleep(min(backoff, 30))
                        continue

            data = await engine.query_stats()
            up_delta = down_delta = 0

            if data:
                merged: dict[tuple[str, str], int] = {}
                for stat in data.get("stat", []):
                    name = stat.get("name", "")
                    value = int(stat.get("value", "0") or "0")
                    if value <= 0:
                        continue
                    parts = name.split(">>>")
                    if len(parts) != 4 or parts[2] != "traffic":
                        continue
                    if parts[0] == "user":
                        key = (parts[1], parts[3])
                        merged[key] = merged.get(key, 0) + value

                with state.lock:
                    for (uid, kind), value in merged.items():
                        if uid not in state.USERS:
                            continue
                        rec = state.STATS["users"].setdefault(
                            uid, {"up": 0, "down": 0}
                        )
                        if kind == "uplink":
                            rec["up"] += value
                            state.STATS["total_up"] += value
                            up_delta += value
                        else:
                            rec["down"] += value
                            state.STATS["total_down"] += value
                            down_delta += value
                        state.LAST_ACTIVE[uid] = time.time()

            ts = int(time.time())
            with state.lock:
                state.STATS["history"].append(
                    [ts, up_delta // config.SAMPLE_SECS, down_delta // config.SAMPLE_SECS]
                )
                # deque(maxlen=...) handles eviction automatically

            changed = (up_delta + down_delta) > 0
            if await _enforce_time_limits():
                changed = True
            if changed:
                state.mark_dirty()

            await _alerts_check()

            # Distribute traffic deltas across active IPs by connection weight
            ip_payload: list = []
            if up_delta > 0 or down_delta > 0:
                with state.lock:
                    active = list(state.ACTIVE_IPS)
                    total_conns = 0
                    for ip in active:
                        c = state.IP_STATS.get(ip, {}).get("conns", 0)
                        if c > 0:
                            total_conns += c
                    if active and total_conns > 0:
                        inv_total = 1.0 / total_conns
                        for ip in active:
                            rec = state.IP_STATS.get(ip)
                            if not rec:
                                continue
                            conns = rec.get("conns", 0)
                            if conns <= 0:
                                continue
                            weight = conns * inv_total
                            us = int(up_delta * weight)
                            ds = int(down_delta * weight)
                            if us:
                                rec["up"] += us
                                rec["up_delta"] += us
                            if ds:
                                rec["down"] += ds
                                rec["down_delta"] += ds
                            rec["conns"] = 0
                            ud = rec["up_delta"]
                            dd = rec["down_delta"]
                            if ud or dd:
                                ip_payload.append(
                                    {
                                        "client_ip": ip,
                                        "up_bytes": ud,
                                        "down_bytes": dd,
                                        "protocol": "ws",
                                    }
                                )
                                rec["up_delta"] = 0
                                rec["down_delta"] = 0

            # Fire-and-forget Axiom shipping (don't block accounting loop)
            # v3.5: Cap payload sizes to prevent RAM spikes with many IPs
            if config.AXIOM_TOKEN:
                logs = engine.get_logs()
                if logs:
                    # Cap at 50 logs per cycle to avoid huge payloads
                    _spawn(_safe_send_to_axiom(logs[:50], "xray_log"))
                if ip_payload:
                    # Cap at 50 IP records per cycle
                    _spawn(_safe_send_to_axiom(ip_payload[:50], "ip_traffic"))

        except Exception as e:
            state.log_error(f"accounting: {e}")
        await asyncio.sleep(config.SAMPLE_SECS)
