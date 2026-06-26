"""Service layer: unified user mutation logic.

v3: Previously, user create/edit/delete/reset logic was duplicated between
server.py (web panel) and bot.py (Telegram). This module centralizes it so
both entry points share the exact same validation, locking, persistence,
and engine-sync behavior.
"""
from __future__ import annotations

import secrets
import time
import uuid

from .. import config, engine, state, storage


def _new_uid() -> str:
    return str(uuid.uuid4())


_active = engine._active


def _sanitize_protos(protos: list[str] | None) -> list[str]:
    if not protos:
        return ["ws", "grpc", "reality"]
    filtered = [p for p in protos if p in config.PROTOCOLS]
    return filtered or ["ws", "grpc", "reality"]


async def create_user(
    *,
    label: str = "کاربر",
    days: int = 0,
    gb: float = 0,
    protos: list[str] | None = None,
    ws_ips: str = "",
    reality_sni: str = "",
    uid: str | None = None,
) -> tuple[str, dict]:
    """Create a new user. Returns (uid, record)."""
    uid = (uid or _new_uid()).strip()
    protos = _sanitize_protos(protos)
    rec = {
        "label": (label or "کاربر").strip()[:48],
        "sid": secrets.token_hex(6),
        "created": int(time.time()),
        "status": "active",
        "expiry": int(time.time() + days * 86400) if days > 0 else 0,
        "quota": int(gb * 1024 ** 3) if gb > 0 else 0,
        "protocols": protos,  # already sanitized above
        "ws_ips": ws_ips.strip(),
        "reality_sni": reality_sni.strip(),
    }
    with state.lock:
        state.USERS[uid] = rec
        state.STATS["users"].setdefault(uid, {"up": 0, "down": 0})
        state.add_sid(rec["sid"], uid)
    storage.save_local()
    state.mark_dirty()
    ok = await engine.hot_add(uid)
    if not ok:
        engine.mark_resync()
    return uid, rec


async def edit_user(
    uid: str,
    *,
    label: str | None = None,
    add_days: int | None = None,
    gb: float | None = None,
    protos: list[str] | None = None,
    ws_ips: str | None = None,
    reality_sni: str | None = None,
    status: str | None = None,
) -> dict | None:
    """Edit an existing user. Returns the updated record or None if not found."""
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return None
        if label is not None:
            u["label"] = label.strip()[:48]
        if add_days:
            base = max(u.get("expiry", 0), time.time())
            u["expiry"] = int(base + int(add_days) * 86400)
            if u.get("status") == "expired":
                u["status"] = "active"
        if gb is not None:
            u["quota"] = int(gb * 1024 ** 3) if gb > 0 else 0
        if protos is not None:
            u["protocols"] = _sanitize_protos(protos)
        if ws_ips is not None:
            u["ws_ips"] = ws_ips.strip()
        if reality_sni is not None:
            u["reality_sni"] = reality_sni.strip()
        if status is not None and status in ("active", "disabled"):
            u["status"] = status
        result = dict(u)
    storage.save_local()
    state.mark_dirty()
    await engine.apply_user(uid)
    return result


async def reset_traffic(uid: str) -> bool:
    """Reset a user's traffic counters. Returns False if user not found."""
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return False
        state.STATS["users"][uid] = {"up": 0, "down": 0}
        if u.get("status") == "expired" and not (
            u.get("expiry") and time.time() > u["expiry"]
        ):
            u["status"] = "active"
        state.clear_user_alerts(uid)
    storage.save_local()
    state.mark_dirty()
    await engine.apply_user(uid)
    return True


async def delete_user(uid: str) -> bool:
    """Delete a user. Returns False if user didn't exist."""
    with state.lock:
        u = state.USERS.pop(uid, None)
        state.STATS["users"].pop(uid, None)
        state.LAST_ACTIVE.pop(uid, None)
        if u:
            state.remove_sid(u.get("sid"))
        state.clear_user_alerts(uid)
    if not u:
        return False
    storage.save_local()
    state.mark_dirty()
    await engine.hot_remove(uid)
    return True


async def add_days(uid: str, days: int) -> bool:
    """Extend a user's expiry. Reactivates expired users."""
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return False
        base = max(u.get("expiry", 0), time.time())
        u["expiry"] = int(base + days * 86400)
        if u.get("status") == "expired":
            u["status"] = "active"
        state.clear_user_alerts(uid)
    storage.save_local()
    state.mark_dirty()
    await engine.apply_user(uid)
    return True


async def add_gb(uid: str, gb: float) -> bool:
    """Add quota to a user. Reactivates expired users."""
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return False
        cur = u.get("quota", 0)
        u["quota"] = (cur + int(gb * 1024 ** 3)) if cur else int(gb * 1024 ** 3)
        if u.get("status") == "expired":
            u["status"] = "active"
        state.clear_user_alerts(uid)
    storage.save_local()
    state.mark_dirty()
    await engine.apply_user(uid)
    return True


async def rename_user(uid: str, label: str) -> bool:
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return False
        u["label"] = (label or "").strip()[:48] or u.get("label", "کاربر")
    storage.save_local()
    state.mark_dirty()
    return True


async def toggle_user(uid: str) -> bool:
    """Toggle active/disabled status."""
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return False
        u["status"] = "disabled" if u.get("status") != "disabled" else "active"
    storage.save_local()
    state.mark_dirty()
    await engine.apply_user(uid)
    return True


def find_users(q: str) -> list[tuple[str, dict]]:
    """Search users by label or uid. Returns up to 20 matches."""
    q = q.strip().lower()
    with state.lock:
        return [
            (uid, u)
            for uid, u in state.USERS.items()
            if q in u.get("label", "").lower() or q in uid.lower()
        ][:20]
