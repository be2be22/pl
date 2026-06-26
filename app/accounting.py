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
_PROXY_IP_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)$")


def _read_log_tail(filepath: str, max_bytes: int) -> list[str]:
    """Read the TAIL of the access log (newest entries), then truncate."""
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

    # Nginx proxy log (real client IPs for WS/gRPC through nginx)
    for line in _read_log_tail(config.PROXY_ACCESS_LOG, config.IP_LOG_MAX_BYTES):
        line = line.strip()
        if not line:
            continue
        m = _PROXY_IP_RE.match(line)
        if m:
            ip = m.group(1)
            if ip != "127.0.0.1" and not ip.startswith("100.64.") and ip != "-":
                seen[ip] = seen.get(ip, 0) + 1

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
            state.ACTIVE_IPS.add(ip)


async def _ip_tracker_loop() -> None:
    """Fast loop (2s) for real-time IP tracking + immediate RAM cleanup."""
    while True:
        try:
            _parse_realtime_ips()
            now = time.time()
            with state.lock:
                # Remove IPs not seen within ONLINE_WINDOW
                stale = [
                    ip
                    for ip in state.ACTIVE_IPS
                    if now - state.IP_STATS.get(ip, {}).get("last", 0)
                    > config.ONLINE_WINDOW
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
                # Evict stale IP_STATS entries (not just ACTIVE_IPS)
                stale_ips = [
                    ip
                    for ip, rec in state.IP_STATS.items()
                    if now - rec.get("last", 0) > config.ONLINE_WINDOW * 2
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
