"""Centralized configuration for Aurora Edge Dashboard v3.

All magic numbers, timeouts, and tunable parameters live here so they can be
adjusted via environment variables without touching application code.
"""
from __future__ import annotations

import os
import secrets as _secrets
import sys
from pathlib import Path


# ── env helpers ──────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# ── control plane ────────────────────────────────────────────────────
CP_PORT: int = 5000
API_PORT: int = 10085
WS_PORT: int = 18080
WS_PATH: str = _env("WS_PATH", "/ws")

# gRPC transport (VLESS over gRPC + TLS)
# Benefits over WS: multiplexed streams, lower latency on lossy networks,
# better performance on mobile (single TCP connection, no per-message framing).
GRPC_PORT: int = 18081
GRPC_PATH: str = _env("GRPC_PATH", "/grpc")  # serviceName in gRPC path

# v3.5: Extra Reality ports removed (caused complexity and RAM overhead).
# If you need alternate ports, use a reverse proxy or Railway TCP proxy manually.

# ── admin auth ───────────────────────────────────────────────────────
# SECURITY: no default password. Startup fails if ADMIN_PASSWORD not set
# AND no persisted hash exists yet (first-run).
ADMIN_PASS: str = _env("ADMIN_PASSWORD")
ADMIN_PATH: str = _env("ADMIN_PATH", "manage").strip("/") or "manage"
SESSION_TTL: int = _int("SESSION_TTL_HOURS", 2) * 3600
SESSION_IDLE_TTL: int = _int("SESSION_IDLE_TTL_MINUTES", 30) * 60  # sliding window

PUBLIC_HOST: str = _env("PUBLIC_HOST")
MASTER_UUID: str = _env("UUID") or _env("MASTER_UUID")

# ── Xray core ────────────────────────────────────────────────────────
CORE_BIN: str = _env("CORE_BIN", "/usr/local/bin/core")
REALITY_APP_PORT: int = _int("RAILWAY_TCP_APPLICATION_PORT", 18443)
TCP_PROXY_DOMAIN: str = _env("RAILWAY_TCP_PROXY_DOMAIN")
TCP_PROXY_PORT: str = _env("RAILWAY_TCP_PROXY_PORT", "18443")
# v3.3: default Reality dest changed to www.microsoft.com (Azure CDN with
# global PoPs, often lower latency than Cloudflare from Iran).
# Alternatives to try (set via env): www.apple.com, www.tesla.com, www.samsung.com
REALITY_DEST: str = _env("REALITY_DEST", "www.microsoft.com:443")
XRAY_VERSION: str = _env("XRAY_VERSION", "v25.3.6")  # pinned for reproducibility

# ── GitHub state sync ────────────────────────────────────────────────
GH_TOKEN: str = _env("GH_TOKEN") or _env("GITHUB_TOKEN")
GH_REPO: str = _env("GH_REPO")
GH_BRANCH: str = _env("GH_BRANCH", "main")
GH_DATA_DIR: str = _env("GH_DATA_DIR", "state").strip("/")
GH_SYNC_SECS: int = max(30, _int("GH_SYNC_SECONDS", 90))

_dep_owner = _env("RAILWAY_GIT_REPO_OWNER")
_dep_name = _env("RAILWAY_GIT_REPO_NAME")
DEPLOY_REPO: str = f"{_dep_owner}/{_dep_name}" if (_dep_owner and _dep_name) else ""

GH_SAME_AS_DEPLOY: bool = bool(
    GH_REPO and DEPLOY_REPO and GH_REPO.strip().lower() == DEPLOY_REPO.strip().lower()
)

# ── Telegram ─────────────────────────────────────────────────────────
TG_TOKEN: str = _env("TG_BOT_TOKEN")
TG_ADMIN_ID: str = _env("TG_ADMIN_ID")
# SECURITY: no default webhook secret. Bot disabled if not set & no MASTER_UUID.
TG_WEBHOOK_SECRET: str = _env("TG_WEBHOOK_SECRET") or (
    MASTER_UUID[:32] if MASTER_UUID else ""
)
TG_WEBHOOK_CONCURRENCY: int = _int("TG_WEBHOOK_CONCURRENCY", 5)

# ── data paths ───────────────────────────────────────────────────────
DATA_DIR: str = _env("DATA_DIR", "/app/data")
USERS_FILE: str = str(Path(DATA_DIR) / "users.json")
STATS_FILE: str = str(Path(DATA_DIR) / "stats.json")
CORE_CFG: str = str(Path(DATA_DIR) / "core.json")
CORE_CFG_ACCESS_LOG: str = _env("CORE_CFG_ACCESS_LOG", str(Path(DATA_DIR) / "access.log"))

# ── accounting loop ──────────────────────────────────────────────────
SAMPLE_SECS: int = max(3, _int("SAMPLE_SECS", 10))
# ONLINE_WINDOW must be >= 2*SAMPLE_SECS to avoid gaps; auto-correct if needed
ONLINE_WINDOW: int = max(2 * SAMPLE_SECS, _int("ONLINE_WINDOW", 30))
HISTORY_LEN: int = _int("HISTORY_LEN", 60)

# ── IP tracking ──────────────────────────────────────────────────────
# v3.5: Reduced MAX_TRACKED_IPS from 500 to 200 to save RAM.
# Each IP record uses ~200 bytes, so 200 IPs = ~40KB (was 100KB).
# 200 is enough for most panels; overflow evicts oldest automatically.
MAX_TRACKED_IPS: int = _int("MAX_TRACKED_IPS", 200)
# v3.5: Reduced tail read from 1MB to 128KB (was causing RAM spikes)
IP_LOG_MAX_BYTES: int = _int("IP_LOG_MAX_BYTES", 131072)  # 128KB tail read
IP_TRACKER_INTERVAL: int = _int("IP_TRACKER_INTERVAL", 2)
IP_EVICT_POLICY: str = _env("IP_EVICT_POLICY", "oldest")  # oldest | lowest_traffic

# ── Axiom logging ────────────────────────────────────────────────────
AXIOM_TOKEN: str = _env("AXIOM_TOKEN")
AXIOM_DATASET: str = _env("AXIOM_DATASET", "aurora-logs")
AXIOM_CACHE_TTL: int = _int("AXIOM_CACHE_TTL", 60)

# ── rate limiting ────────────────────────────────────────────────────
RATE_LIMIT_LOGIN: int = _int("RATE_LIMIT_LOGIN", 6)
RATE_LIMIT_MUTATE: int = _int("RATE_LIMIT_MUTATE", 30)
RATE_LIMIT_TG: int = _int("RATE_LIMIT_TG", 30)
MAX_RATE_ENTRIES: int = _int("MAX_RATE_ENTRIES", 500)

# ── alerts ───────────────────────────────────────────────────────────
ALERT_MEM_PCT: float = _float("ALERT_MEM_PCT", 90.0)
ALERT_CPU_PCT: float = _float("ALERT_CPU_PCT", 90.0)
ALERT_EXPIRY_DAYS: int = _int("ALERT_EXPIRY_DAYS", 3)

# ── cleanup intervals ────────────────────────────────────────────────
CLEANUP_INTERVAL: int = _int("CLEANUP_INTERVAL", 600)  # 10 min
SESSION_GC_INTERVAL: int = _int("SESSION_GC_INTERVAL", 300)  # 5 min

# ── HTTP client pools ────────────────────────────────────────────────
HTTP_IDLE_TIMEOUT: float = _float("HTTP_IDLE_TIMEOUT", 300.0)
HTTP_TIMEOUT_GH: float = _float("HTTP_TIMEOUT_GH", 15.0)
HTTP_TIMEOUT_AXIOM: float = _float("HTTP_TIMEOUT_AXIOM", 10.0)
HTTP_TIMEOUT_TG: float = _float("HTTP_TIMEOUT_TG", 15.0)

# ── engine ───────────────────────────────────────────────────────────
ENGINE_SPAWN_WAIT: float = _float("ENGINE_SPAWN_WAIT", 1.5)
ENGINE_RESYNC_BACKOFF_MAX: int = _int("ENGINE_RESYNC_BACKOFF_MAX", 60)
ENGINE_RESYNC_BACKOFF_BASE: int = _int("ENGINE_RESYNC_BACKOFF_BASE", 2)

# ── protocols ────────────────────────────────────────────────────────
# Supported transport protocols:
#   ws      → VLESS + WebSocket + TLS        (legacy, widely supported)
#   grpc    → VLESS + gRPC + TLS             (faster, multiplexed, mobile-friendly)
#   reality → VLESS + Reality (uTLS)         (no TLS cert needed, anti-detection)
PROTOCOLS: frozenset[str] = frozenset({"ws", "grpc", "reality"})

# ── derived ──────────────────────────────────────────────────────────
def reality_servernames() -> list[str]:
    host = REALITY_DEST.split(":")[0]
    return [host]


def gh_disabled_reason() -> str:
    if not GH_TOKEN:
        return "GH_TOKEN تنظیم نشده"
    if "/" not in GH_REPO:
        return "GH_REPO باید به شکل owner/name باشد"
    if GH_SAME_AS_DEPLOY:
        return "GH_REPO همان ریپوی دیپلوی است."
    return ""


def gh_enabled() -> bool:
    return not gh_disabled_reason()


def tg_enabled() -> bool:
    return bool(TG_TOKEN and TG_ADMIN_ID and TG_WEBHOOK_SECRET)


def startup_health_check() -> list[str]:
    """Return list of critical issues. Non-empty = refuse to start."""
    issues: list[str] = []
    if not ADMIN_PASS:
        # On first run with no persisted hash, we need a password.
        # But we can't check state here (circular import). server.lifespan handles it.
        pass
    if not TG_WEBHOOK_SECRET and (TG_TOKEN or TG_ADMIN_ID):
        issues.append(
            "TG_BOT_TOKEN/TG_ADMIN_ID تنظیم شده اما TG_WEBHOOK_SECRET خالی است. "
            "یا MASTER_UUID تنظیم کنید یا TG_WEBHOOK_SECRET."
        )
    return issues


# NOTE: do NOT makedirs here (side effect at import time is fragile).
# server.lifespan handles directory creation on startup.
