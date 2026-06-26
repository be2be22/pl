"""Session auth, password handling (bcrypt), and in-memory rate limiter.

v3 changes:
  - bcrypt replaces SHA256 (slow KDF, resistant to brute-force).
  - Sliding session expiration: TTL refreshes on each valid request.
  - Rate limiter GC runs on a timer (not just on overflow).
  - All dict access is lock-protected.
"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Optional

import bcrypt

from . import config, state


# ── password (bcrypt) ────────────────────────────────────────────────
def _hash(pw: str) -> str:
    """bcrypt hash with cost factor 12 (sufficient as of 2025)."""
    return bcrypt.hashpw(
        pw.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("ascii")


def verify_password(pw: str) -> bool:
    """Verify against persisted bcrypt hash, or env fallback on first run."""
    admin = state.STATS.get("admin") or {}
    stored_hash = admin.get("hash")
    if stored_hash:
        try:
            return bcrypt.checkpw(
                pw.encode("utf-8"), stored_hash.encode("ascii")
            )
        except (ValueError, TypeError):
            # Corrupted hash — fall through to env check
            pass
    # First-run fallback: compare against env ADMIN_PASS (constant-time)
    if config.ADMIN_PASS:
        return hmac.compare_digest(pw, config.ADMIN_PASS)
    return False


def has_persisted_password() -> bool:
    admin = state.STATS.get("admin") or {}
    return bool(admin.get("hash"))


def set_password(new_pw: str) -> None:
    with state.lock:
        state.STATS["admin"] = {"hash": _hash(new_pw)}
    state.mark_dirty()


# ── sessions (sliding window + idle timeout) ─────────────────────────
# Each session tracks both absolute expiry and last-activity timestamp.
# SESSIONS[token] = [absolute_expiry, last_activity_ts]
def open_session() -> str:
    """Create a new session. Returns the opaque token."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with state.lock:
        state.SESSIONS[token] = [now + config.SESSION_TTL, now]
    return token


def valid_session(token: Optional[str]) -> bool:
    """Check session validity.

    Two-layer expiry:
      1. Absolute TTL (SESSION_TTL) — hard limit from login time, refreshed
         on each valid request (sliding window).
      2. Idle TTL (SESSION_IDLE_TTL) — session expires if no activity for
         this long, even if absolute TTL hasn't been reached.
    """
    if not token:
        return False
    now = time.time()
    with state.lock:
        rec = state.SESSIONS.get(token)
        if not rec:
            return False
        # Migration safety: old format was float, new format is [abs_exp, last_activity]
        if isinstance(rec, (int, float)):
            # Old-format session: validate by absolute expiry only, then upgrade
            if now > float(rec):
                state.SESSIONS.pop(token, None)
                return False
            state.SESSIONS[token] = [now + config.SESSION_TTL, now]
            return True
        abs_exp, last_activity = rec[0], rec[1]
        # Hard expiry
        if now > abs_exp:
            state.SESSIONS.pop(token, None)
            return False
        # Idle expiry (prevents stolen-cookie indefinite refresh)
        if now - last_activity > config.SESSION_IDLE_TTL:
            state.SESSIONS.pop(token, None)
            return False
        # Sliding: refresh both absolute expiry and last-activity on success
        state.SESSIONS[token] = [now + config.SESSION_TTL, now]
    return True


def close_session(token: Optional[str]) -> None:
    if token:
        with state.lock:
            state.SESSIONS.pop(token, None)


def gc_sessions() -> None:
    """Purge expired or idle-timed-out sessions."""
    now = time.time()
    with state.lock:
        dead = []
        for t, rec in state.SESSIONS.items():
            if isinstance(rec, (int, float)):
                # Old format: absolute expiry only
                if now > float(rec):
                    dead.append(t)
            elif isinstance(rec, list) and len(rec) >= 2:
                # New format: [abs_exp, last_activity]
                if (now > rec[0]) or (now - rec[1] > config.SESSION_IDLE_TTL):
                    dead.append(t)
        for t in dead:
            state.SESSIONS.pop(t, None)


# ── rate limiter (sliding fixed window) ──────────────────────────────
def gc_rate() -> None:
    """Purge expired rate-limiter entries to prevent unbounded growth."""
    now = time.time()
    with state.lock:
        dead = [k for k, v in state.RATE.items() if now - v[1] > 3600]
        for k in dead:
            state.RATE.pop(k, None)


def allow(ip: str, bucket: str, limit: int = 8, window: int = 30) -> bool:
    """Sliding fixed-window rate limiter.

    Returns True if request is allowed, False if limit exceeded.
    Auto-evicts expired entries when the table exceeds MAX_RATE_ENTRIES.
    """
    key = f"{ip}:{bucket}"
    now = time.time()
    with state.lock:
        # Periodic eviction when table is large
        if len(state.RATE) > config.MAX_RATE_ENTRIES:
            expired = [
                k for k, v in state.RATE.items() if now - v[1] > window
            ]
            for k in expired:
                state.RATE.pop(k, None)
            # If still over limit, evict oldest entries
            if len(state.RATE) > config.MAX_RATE_ENTRIES:
                oldest = sorted(
                    state.RATE.items(), key=lambda x: x[1][1]
                )[: len(state.RATE) - config.MAX_RATE_ENTRIES]
                for k, _ in oldest:
                    state.RATE.pop(k, None)

        rec = state.RATE.get(key)
        if not rec or now - rec[1] > window:
            state.RATE[key] = [1, now]
            return True
        if rec[0] >= limit:
            return False
        rec[0] += 1
        return True
