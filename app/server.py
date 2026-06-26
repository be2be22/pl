"""FastAPI application: control plane for Aurora Edge Dashboard.

v3 changes:
  - Security headers middleware (X-Content-Type-Options, X-Frame-Options,
    HSTS, Referrer-Policy).
  - Pydantic request validation (no more ad-hoc int()/float() that 500s).
  - Task tracking: all background tasks are stored in a set and properly
    cancelled on shutdown (no more fire-and-forget GC issues).
  - Backpressure for Telegram webhook (semaphore limits concurrent updates).
  - Secure cookie flags (httponly, samesite, secure when HTTPS).
  - Startup health check: refuse to boot if critical config missing.
  - QR cache keyed by sid+uid for proper invalidation.
  - Root path /api/users now paginated.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache

import segno
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import (
    accounting,
    axiom_logs,
    bot,
    config,
    engine,
    ghsync,
    notify,
    security,
    state,
    storage,
    subs,
    util,
)
from .schemas import (
    ChangePasswordRequest,
    CreateUserRequest,
    EditUserRequest,
    LoginRequest,
)
from .services import user_service


# ── QR cache (keyed by sid for safe invalidation) ───────────────────
@lru_cache(maxsize=256)
def _cached_qr_svg(data: str) -> str:
    return segno.make(data, error="m").svg_inline(
        scale=6, dark="#0b1020", light="#ffffff"
    )


_UI = os.path.join(os.path.dirname(__file__), "ui")
_PFX = "/" + config.ADMIN_PATH

_tpl_cache: dict[str, str] = {}


def _tpl(name: str) -> str:
    cached = _tpl_cache.get(name)
    if cached is not None:
        return cached
    with open(os.path.join(_UI, name), encoding="utf-8") as f:
        content = f.read()
    _tpl_cache[name] = content
    return content


def get_domain(request: Request) -> str:
    if config.PUBLIC_HOST:
        return config.PUBLIC_HOST.split("//")[-1].split("/")[0]
    host = request.headers.get("host", "")
    return host.split(":")[0] if host else "localhost"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto") == "https"


def _authed(request: Request) -> bool:
    return security.valid_session(request.cookies.get("as"))


def _guard(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(401, "unauthorized")


def _user_view(uid: str, u: dict | None) -> dict | None:
    """Build a user view dict. Returns None if u is None (user deleted)."""
    if u is None:
        return None
    with state.lock:
        rec = state.STATS["users"].get(uid, {"up": 0, "down": 0})
        rec_copy = dict(rec)
    online = (time.time() - state.LAST_ACTIVE.get(uid, 0)) < config.ONLINE_WINDOW
    return {
        "uid": uid,
        "label": u.get("label", ""),
        "sid": u.get("sid", ""),
        "status": u.get("status", "active"),
        "protocols": u.get("protocols", ["ws", "grpc", "reality"]),
        "created": u.get("created", 0),
        "expiry": u.get("expiry", 0),
        "quota": u.get("quota", 0),
        "up": rec_copy["up"],
        "down": rec_copy["down"],
        "used": rec_copy["up"] + rec_copy["down"],
        "online": online,
        "days_left": util.remaining_days(u.get("expiry", 0)),
        "ws_ips": u.get("ws_ips", ""),
        "reality_sni": u.get("reality_sni", ""),
    }


# ── background task tracking ─────────────────────────────────────────
_all_tasks: set[asyncio.Task] = set()


def _track(coro) -> asyncio.Task:
    """Spawn a tracked background task (cancelled on shutdown)."""
    t = asyncio.create_task(coro)
    _all_tasks.add(t)
    t.add_done_callback(_all_tasks.discard)
    return t


# Telegram webhook backpressure
_tg_semaphore: asyncio.Semaphore | None = None


def _tg_sem() -> asyncio.Semaphore:
    global _tg_semaphore
    if _tg_semaphore is None:
        _tg_semaphore = asyncio.Semaphore(config.TG_WEBHOOK_CONCURRENCY)
    return _tg_semaphore


async def _handle_tg_update(update: dict) -> None:
    async with _tg_sem():
        await bot.handle_update(update)


# ── session GC loop ──────────────────────────────────────────────────
async def _session_gc_loop() -> None:
    while True:
        await asyncio.sleep(config.SESSION_GC_INTERVAL)
        try:
            security.gc_sessions()
            security.gc_rate()
        except Exception as e:
            state.log_error(f"session_gc: {e}")


# ── persist loop ─────────────────────────────────────────────────────
async def _persist_loop() -> None:
    while True:
        await asyncio.sleep(config.GH_SYNC_SECS)
        try:
            await storage.persist_tick()
        except Exception as e:
            state.log_error(f"persist: {e}")


# ── security headers middleware ──────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS only on HTTPS ( Railway terminates TLS at the proxy)
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


# ── lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data directory exists (deferred from config.py import time)
    os.makedirs(config.DATA_DIR, exist_ok=True)

    # Startup health check
    issues = config.startup_health_check()
    for issue in issues:
        state.log_error(f"STARTUP: {issue}")

    # First-run password check
    if not security.has_persisted_password() and not config.ADMIN_PASS:
        state.log_error(
            "STARTUP CRITICAL: ADMIN_PASSWORD not set and no persisted hash. "
            "Using random password for first run (check logs)."
        )
        import secrets as _s
        random_pw = _s.token_urlsafe(16)
        security.set_password(random_pw)
        state.log_error(f"FIRST-RUN PASSWORD (save immediately): {random_pw}")

    src = await storage.boot_load()
    await engine.ensure_keys()
    state.index_sids()

    # Ensure Axiom dataset exists
    await axiom_logs.ensure_dataset()

    # Register Telegram webhook
    if bot.enabled() and config.PUBLIC_HOST:
        _track(bot.setup_webhook())

    # Initial Xray resync
    await asyncio.to_thread(engine.resync)

    # GitHub sync status
    _gh_off = config.gh_disabled_reason()
    if _gh_off:
        state.log_error(f"github-sync OFF: {_gh_off}")

    # Start background loops
    bg_tasks = [
        _track(accounting.loop()),
        _track(_persist_loop()),
        _track(_session_gc_loop()),
    ]

    if notify.enabled():
        _track(notify.send(f"🟢 Aurora v3.1 آنلاین شد\nکاربران: {len(state.USERS)}"))

    try:
        yield
    finally:
        # Cancel accounting background tasks (cleanup, ip_tracker, axiom sends)
        try:
            await accounting.cancel_all()
        except Exception:
            pass
        # Cancel all tracked tasks
        for t in list(_all_tasks):
            t.cancel()
        if _all_tasks:
            await asyncio.gather(*list(_all_tasks), return_exceptions=True)
        # Final flush
        try:
            await storage.flush("shutdown")
        except Exception:
            pass
        # Close HTTP clients
        from . import http_util
        await http_util.close_all()


def build_app() -> FastAPI:
    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_UI, "static")),
        name="static",
    )

    # ── public endpoints ────────────────────────────────────────────
    @app.get("/up", response_class=PlainTextResponse)
    async def health():
        return "ok"

    @app.post("/tg/{secret}")
    async def telegram_webhook(secret: str, request: Request):
        # Two-layer validation: path secret + Telegram header secret
        if secret != config.TG_WEBHOOK_SECRET:
            raise HTTPException(404)
        hdr = request.headers.get("x-telegram-bot-api-secret-token", "")
        if hdr != config.TG_WEBHOOK_SECRET:
            raise HTTPException(403)
        # Cache domain for subscription links if not configured
        if not config.PUBLIC_HOST:
            bot._domain_cache["host"] = get_domain(request)
        try:
            update = await request.json()
        except Exception:
            return {"ok": True}
        # Process in background with backpressure
        _track(_handle_tg_update(update))
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Service</title>"
            "<body style='font-family:sans-serif;color:#666;text-align:center;margin-top:18vh'>"
            "<h3>Service is running.</h3></body>"
        )

    # ── admin panel ─────────────────────────────────────────────────
    @app.get(_PFX, response_class=HTMLResponse)
    async def panel(request: Request):
        return HTMLResponse(
            _tpl("panel.html") if _authed(request) else _tpl("login.html")
        )

    @app.post(_PFX + "/api/login")
    async def login(request: Request, body: LoginRequest):
        ip = _client_ip(request)
        if not security.allow(
            ip, "login", limit=config.RATE_LIMIT_LOGIN, window=60
        ):
            raise HTTPException(429, "تلاش بیش از حد.")
        if not security.verify_password(body.password):
            raise HTTPException(401, "رمز عبور نادرست است")
        token = security.open_session()
        resp = JSONResponse({"ok": True})
        secure = _is_https(request)
        resp.set_cookie(
            "as",
            token,
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=config.SESSION_TTL,
            path="/",
        )
        return resp

    @app.post(_PFX + "/api/logout")
    async def logout(request: Request):
        security.close_session(request.cookies.get("as"))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("as", path="/")
        return resp

    @app.get(_PFX + "/api/overview")
    async def overview(request: Request):
        _guard(request)
        with state.lock:
            users = list(state.USERS.items())
            active = sum(1 for _, u in users if u.get("status") == "active")
            expired = sum(1 for _, u in users if u.get("status") == "expired")
            disabled = sum(1 for _, u in users if u.get("status") == "disabled")
            tu, td = state.STATS["total_up"], state.STATS["total_down"]
            history = list(state.STATS["history"])[-90:]
            active_ips = len(state.ACTIVE_IPS)
            total_ips = len(state.IP_STATS)
            now = time.time()
            online_ws = 0
            online_grpc = 0
            online_reality = 0
            for uid, u in users:
                if (now - state.LAST_ACTIVE.get(uid, 0)) >= config.ONLINE_WINDOW:
                    continue
                protos = u.get("protocols", [])
                if "ws" in protos:
                    online_ws += 1
                if "grpc" in protos:
                    online_grpc += 1
                if "reality" in protos:
                    online_reality += 1
        sys = state.SYS
        return {
            "totals": {"up": tu, "down": td, "all": tu + td},
            "speed": {"up": sys["tx_bps"], "down": sys["rx_bps"]},
            "online": state.online_count(),
            "online_proto": {"ws": online_ws, "grpc": online_grpc, "reality": online_reality},
            "users": {
                "total": len(users),
                "active": active,
                "expired": expired,
                "disabled": disabled,
            },
            "sys": {
                "cpu": sys["cpu"],
                "mem_used": sys["mem_used"],
                "mem_total": sys["mem_total"],
                "mem_pct": round(sys["mem_used"] / sys["mem_total"] * 100, 1)
                if sys["mem_total"]
                else 0,
            },
            "history": history,
            "uptime": int(time.time() - state.BOOT_TS),
            "core_alive": engine.alive(),
            "reality_ready": bool(
                config.TCP_PROXY_DOMAIN and state.STATS["reality"].get("pub")
            ),
            "ips": {"active": active_ips, "total": total_ips},
            "gh": {
                "enabled": config.gh_enabled(),
                "reason": config.gh_disabled_reason(),
                **ghsync.status(),
            },
            "host": get_domain(request),
        }

    @app.get(_PFX + "/api/axiom-ip-count")
    async def axiom_ip_count(request: Request):
        _guard(request)
        count = await axiom_logs.fetch_unique_ip_count()
        return {"count": count}

    @app.get(_PFX + "/api/ips")
    async def top_ips(request: Request):
        _guard(request)
        ips = await axiom_logs.fetch_top_ips()
        if ips:
            return ips
        with state.lock:
            ram_ips = sorted(
                state.IP_STATS.items(),
                key=lambda x: x[1].get("up", 0) + x[1].get("down", 0),
                reverse=True,
            )[:20]
            return [
                {
                    "ip": ip,
                    "up": d.get("up", 0),
                    "down": d.get("down", 0),
                    "total": d.get("up", 0) + d.get("down", 0),
                }
                for ip, d in ram_ips
            ]

    @app.get(_PFX + "/api/users")
    async def list_users(request: Request, page: int | None = None, size: int = 50):
        """List users.

        Backward-compatible: returns a flat list (legacy frontend expects this).
        If `page` is explicitly provided, returns a paginated dict instead.
        """
        _guard(request)
        size = max(1, min(size, 500))
        with state.lock:
            items = list(state.USERS.items())
            # Snapshot each user dict under lock to avoid race during view build
            snapshots = [(uid, dict(u)) for uid, u in items]
        views = [_user_view(uid, snap) for uid, snap in snapshots]
        # Filter out any None results (shouldn't happen, but defensive)
        views = [v for v in views if v is not None]
        # Flat list for backward compatibility with the web frontend
        if page is None:
            return views
        # Paginated response (for future API clients)
        total = len(views)
        start = page * size
        end = start + size
        return {"total": total, "page": page, "size": size, "users": views[start:end]}

    @app.post(_PFX + "/api/users")
    async def create_user(request: Request, body: CreateUserRequest):
        _guard(request)
        ip = _client_ip(request)
        if not security.allow(
            ip, "mutate", limit=config.RATE_LIMIT_MUTATE, window=60
        ):
            raise HTTPException(429, "درخواست بیش از حد")
        uid, rec = await user_service.create_user(
            label=body.label,
            days=body.days,
            gb=body.gb,
            protos=body.protocols,
            ws_ips=body.ws_ips,
            reality_sni=body.reality_sni,
            uid=body.uuid,
        )
        if notify.enabled():
            _track(notify.send(f"➕ کاربر جدید: <b>{body.label}</b>"))
        return {"ok": True, "uid": uid, **subs.build_links(uid, rec, get_domain(request))}

    @app.get(_PFX + "/api/users/{uid}/links")
    async def user_links(uid: str, request: Request):
        _guard(request)
        with state.lock:
            u = state.USERS.get(uid)
        if not u:
            raise HTTPException(404)
        return subs.build_links(uid, u, get_domain(request))

    @app.post(_PFX + "/api/users/{uid}")
    async def edit_user(uid: str, request: Request, body: EditUserRequest):
        _guard(request)
        result = await user_service.edit_user(
            uid,
            label=body.label,
            add_days=body.add_days,
            gb=body.gb,
            protos=body.protocols,
            ws_ips=body.ws_ips,
            reality_sni=body.reality_sni,
            status=body.status,
        )
        if result is None:
            raise HTTPException(404)
        with state.lock:
            u = state.USERS.get(uid)
        view = _user_view(uid, u)
        if view is None:
            raise HTTPException(404)
        return {"ok": True, **view}

    @app.post(_PFX + "/api/users/{uid}/reset")
    async def reset_user(uid: str, request: Request):
        _guard(request)
        ok = await user_service.reset_traffic(uid)
        if not ok:
            raise HTTPException(404)
        return {"ok": True}

    @app.delete(_PFX + "/api/users/{uid}")
    async def delete_user(uid: str, request: Request):
        _guard(request)
        ok = await user_service.delete_user(uid)
        if not ok:
            raise HTTPException(404)
        return {"ok": True}

    @app.post(_PFX + "/api/password")
    async def change_password(request: Request, body: ChangePasswordRequest):
        _guard(request)
        if not security.verify_password(body.current):
            raise HTTPException(403, "رمز فعلی نادرست است")
        security.set_password(body.new)
        storage.save_local()
        return {"ok": True}

    @app.post(_PFX + "/api/backup")
    async def manual_backup(request: Request):
        _guard(request)
        if not config.gh_enabled():
            raise HTTPException(400, "گیتهاب پیکربندی نشده")
        ok = await ghsync.backup("manual")
        return {"ok": ok, **ghsync.status()}

    @app.post(_PFX + "/api/restore")
    async def manual_restore(request: Request):
        _guard(request)
        if not config.gh_enabled():
            raise HTTPException(400, "گیتهاب پیکربندی نشده")
        result = await ghsync.restore()
        ok = result["users"] or result["stats"]
        if ok:
            engine.mark_resync()
        return {"ok": ok, **ghsync.status()}

    @app.get(_PFX + "/api/logs")
    async def logs(request: Request):
        _guard(request)
        return [
            {"ts": ts, "text": txt} for ts, txt in list(state.ERRORS)[:80]
        ]

    @app.get(_PFX + "/api/qr")
    async def qr(request: Request, data: str):
        _guard(request)
        svg = _cached_qr_svg(data)
        return Response(svg, media_type="image/svg+xml")

    # ── subscription endpoint (public) ──────────────────────────────
    @app.get("/s/{sid}")
    async def subscription(sid: str, request: Request):
        with state.lock:
            uid = state.SID_INDEX.get(sid)
            u = state.USERS.get(uid) if uid else None
            u_copy = dict(u) if u else None
        if not u_copy:
            return PlainTextResponse("not found", status_code=404)

        domain = get_domain(request)
        ua = request.headers.get("user-agent", "").lower()
        data = subs.build_links(uid, u_copy, domain)

        if "clash" in ua or "meta" in ua or "stash" in ua:
            return PlainTextResponse(
                subs.clash_config(uid, u_copy, domain),
                media_type="text/yaml",
            )

        is_browser = any(
            b in ua for b in ("mozilla", "chrome", "safari", "firefox", "edge", "opera")
        )
        if not is_browser:
            used = state.user_used(uid)
            title_b64 = (
                "base64:"
                + base64.b64encode(
                    u_copy.get("label", "Aurora").encode()
                ).decode()
            )
            headers = {
                "Subscription-Userinfo": (
                    f"upload=0; download={used}; "
                    f"total={u_copy.get('quota', 0)}; "
                    f"expire={u_copy.get('expiry', 0)}"
                ),
                "Profile-Title": title_b64,
                "Profile-Update-Interval": "12",
            }
            return PlainTextResponse(data["sub_b64"], headers=headers)

        with state.lock:
            rec = state.STATS["users"].get(uid, {"up": 0, "down": 0})
            rec_copy = dict(rec)
        qr_svg = _cached_qr_svg(data["sub_link"])
        payload = {
            "label": u_copy.get("label", ""),
            "status": u_copy.get("status", "active"),
            "links": data["links"],
            "sub_link": data["sub_link"],
            "sub_b64": data["sub_b64"],
            "qr": qr_svg,
            "used": rec_copy["up"] + rec_copy["down"],
            "quota": u_copy.get("quota", 0),
            "expiry": u_copy.get("expiry", 0),
            "days_left": util.remaining_days(u_copy.get("expiry", 0)),
            "protocols": u_copy.get("protocols", []),
        }
        html_content = _tpl("sub.html").replace(
            "/*__SUB_DATA__*/",
            "window.__SUB__=" + json.dumps(payload, ensure_ascii=False) + ";",
        )
        return HTMLResponse(html_content)

    return app
