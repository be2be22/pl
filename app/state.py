"""Global application state with thread-safe access patterns.

All mutable globals live here. Access is mediated through `lock` (RLock)
for cross-thread safety. History uses deque(maxlen=...) for O(1) bounded append.
"""
from __future__ import annotations

import threading
import time
from collections import deque

# ── core data ────────────────────────────────────────────────────────
USERS: dict[str, dict] = {}

STATS: dict = {
    "total_up": 0,
    "total_down": 0,
    "users": {},
    "reality": {"priv": "", "pub": "", "sid": ""},
    "history": [],  # will be replaced with deque in init_history()
    "first_seen": 0,
    "admin": {},  # {"salt", "hash"} for bcrypt
}

LAST_ACTIVE: dict[str, float] = {}

# ── IP tracking ──────────────────────────────────────────────────────
IP_STATS: dict[str, dict] = {}
ACTIVE_IPS: set[str] = set()

# ── system metrics ───────────────────────────────────────────────────
SYS: dict = {
    "cpu": 0.0,
    "mem_used": 0,
    "mem_total": 0,
    "rx": 0,
    "tx": 0,
    "rx_bps": 0,
    "tx_bps": 0,
}

# ── logs & sessions ──────────────────────────────────────────────────
ERRORS: deque = deque(maxlen=50)
SESSIONS: dict[str, list | float] = {}  # token -> [abs_exp, last_activity] or float (legacy)
RATE: dict[str, list] = {}  # key -> [count, timestamp]

# ── indexes ──────────────────────────────────────────────────────────
SID_INDEX: dict[str, str] = {}

# ── runtime flags ────────────────────────────────────────────────────
BOOT_TS: float = time.time()
ALERTS_SENT: set[str] = set()
LAST_CORE_ALIVE: bool = True

lock = threading.RLock()
_dirty_at: float = 0.0

# ── snapshot caches (for expensive computations) ─────────────────────
_snapshot_online: int = 0
_snapshot_ts: float = 0.0


def init_history(maxlen: int) -> None:
    """Convert STATS['history'] list to deque after restore."""
    existing = STATS.get("history", [])
    if isinstance(existing, deque):
        return
    d: deque = deque(maxlen=maxlen)
    for item in existing[-maxlen:]:
        d.append(item)
    STATS["history"] = d


def index_sids() -> None:
    global SID_INDEX
    with lock:
        SID_INDEX = {
            u.get("sid"): uid
            for uid, u in USERS.items()
            if u.get("sid")
        }


def add_sid(sid: str, uid: str) -> None:
    if sid:
        SID_INDEX[sid] = uid


def remove_sid(sid: str) -> None:
    SID_INDEX.pop(sid, None)


def mark_dirty() -> None:
    global _dirty_at
    if _dirty_at == 0.0:
        _dirty_at = time.time()


def take_dirty() -> bool:
    global _dirty_at
    if _dirty_at:
        _dirty_at = 0.0
        return True
    return False


def is_dirty() -> bool:
    return _dirty_at != 0.0


def log_error(text: str) -> None:
    ERRORS.appendleft((int(time.time()), str(text)[:300]))


def user_used(uid: str) -> int:
    u = STATS["users"].get(uid)
    return (u["up"] + u["down"]) if u else 0


def online_count() -> int:
    """Cached online count (refreshed every 5s)."""
    global _snapshot_online, _snapshot_ts
    now = time.time()
    if now - _snapshot_ts < 5 and _snapshot_ts > 0:
        return _snapshot_online
    with lock:
        now_locked = time.time()
        if now_locked - _snapshot_ts < 5 and _snapshot_ts > 0:
            return _snapshot_online
        _snapshot_online = sum(
            1 for t in LAST_ACTIVE.values() if now_locked - t < 120
        )
        _snapshot_ts = now_locked
    return _snapshot_online


def clear_user_alerts(uid: str) -> None:
    for prefix in ("q80:", "q95:", "exp:"):
        ALERTS_SENT.discard(f"{prefix}{uid}")
