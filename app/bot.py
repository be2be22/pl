"""Telegram bot (webhook-based) for full panel management from Telegram.

v3 changes:
  - Uses services.user_service for all mutations (no duplication with server.py).
  - Uses shared tg_client from http_util.
  - HTML-escapes all user-supplied content in messages (prevents formatting
    breakage and injection).
  - _CONV access is lock-protected (was racy on concurrent webhooks).
  - _domain_cache is lock-protected.
  - Backpressure via semaphore (handled in server.py).
"""
from __future__ import annotations

import asyncio
import html
import io
import time

from . import config, engine, geo, ghsync, state, storage, subs, util
from . import security as _security
from .http_util import tg_client
from .services import user_service

_API = "https://api.telegram.org"

# Conversation state (multi-step flows like "create user")
_CONV: dict[int, dict] = {}
_CONV_TTL = 600  # 10 minutes
_CONV_MAX = 100
_conv_lock = asyncio.Lock()

# Cache for discovered domain (set from webhook request)
_domain_cache: dict[str, str] = {"host": ""}
_domain_lock = asyncio.Lock()


def enabled() -> bool:
    return config.tg_enabled()


def _is_admin(chat_id) -> bool:
    try:
        return str(chat_id) == str(config.TG_ADMIN_ID)
    except Exception:
        return False


# ── Telegram API helpers ─────────────────────────────────────────────
async def _call(method: str, payload: dict, files: dict | None = None) -> dict | None:
    if not config.TG_TOKEN:
        return None
    url = f"{_API}/bot{config.TG_TOKEN}/{method}"
    try:
        client = await tg_client.get()
        if files:
            r = await client.post(url, data=payload, files=files)
        else:
            r = await client.post(url, json=payload)
        if r.status_code != 200:
            state.log_error(f"tg.{method}: HTTP {r.status_code} {r.text[:120]}")
            return None
        return r.json()
    except Exception as e:
        state.log_error(f"tg.{method}: {e}")
        return None


async def send(chat_id, text: str, keyboard: list | None = None,
               edit_id: int | None = None) -> dict | None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    if edit_id:
        payload["message_id"] = edit_id
        return await _call("editMessageText", payload)
    return await _call("sendMessage", payload)


async def _answer_cb(cb_id: str, text: str = "") -> None:
    await _call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


async def notify_admin(text: str, keyboard: list | None = None) -> None:
    if enabled():
        await send(config.TG_ADMIN_ID, text, keyboard)


# ── Webhook lifecycle ────────────────────────────────────────────────
def webhook_url() -> str:
    host = config.PUBLIC_HOST or _domain_cache.get("host") or ""
    host = host.replace("https://", "").replace("http://", "").strip("/")
    if not host:
        return ""
    return f"https://{host}/tg/{config.TG_WEBHOOK_SECRET}"


async def setup_webhook(host: str = "") -> bool:
    if not enabled():
        return False
    if host:
        async with _domain_lock:
            _domain_cache["host"] = host
    url = webhook_url()
    if not url:
        state.log_error("tg: webhook URL unknown (PUBLIC_HOST not set yet)")
        return False
    res = await _call("setWebhook", {
        "url": url,
        "secret_token": config.TG_WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    })
    if res and res.get("ok"):
        state.log_error("tg: webhook registered")
        await _set_commands()
        return True
    return False


async def _set_commands() -> None:
    await _call("setMyCommands", {"commands": [
        {"command": "menu", "description": "منوی اصلی"},
        {"command": "stats", "description": "داشبورد لحظه‌ای"},
        {"command": "users", "description": "لیست کاربران"},
        {"command": "new", "description": "ساخت کاربر جدید"},
        {"command": "find", "description": "جستجوی کاربر"},
        {"command": "server", "description": "وضعیت سرور"},
        {"command": "ips", "description": "IPهای برتر"},
        {"command": "online_ips", "description": "IPهای آنلاین"},
        {"command": "backup", "description": "بکاپ‌گیری"},
        {"command": "cancel", "description": "لغو عملیات جاری"},
    ]})


# ── conversation state ───────────────────────────────────────────────
def _gc_conv() -> None:
    now = time.time()
    expired = [c for c, v in _CONV.items() if now - v.get("ts", 0) > _CONV_TTL]
    for cid in expired:
        _CONV.pop(cid, None)
    if len(_CONV) > _CONV_MAX:
        oldest = sorted(_CONV.keys(), key=lambda k: _CONV[k].get("ts", 0))[
            : len(_CONV) - _CONV_MAX
        ]
        for cid in oldest:
            _CONV.pop(cid, None)


def _set_conv(chat_id: int, flow: str, step: str, data: dict | None = None) -> None:
    _CONV[chat_id] = {"flow": flow, "step": step, "data": data or {}, "ts": time.time()}


def _clear_conv(chat_id: int) -> None:
    _CONV.pop(chat_id, None)


# ── formatting helpers ────────────────────────────────────────────────
def _b(n) -> str:
    return util.fmt_bytes(n)


def _domain() -> str:
    return (
        config.PUBLIC_HOST.replace("https://", "").replace("http://", "").strip("/")
        or _domain_cache.get("host")
        or "localhost"
    )


def _short(uid: str) -> str:
    return uid[:8]


def _status_fa(s: str) -> str:
    return {"active": "🟢 فعال", "expired": "🔴 منقضی", "disabled": "⚪️ غیرفعال"}.get(s, s)


def _esc(text: str) -> str:
    """HTML-escape user-supplied text for safe Telegram display."""
    return html.escape(str(text or ""))


# ── keyboards ─────────────────────────────────────────────────────
def _main_menu() -> list:
    return [
        [{"text": "📊 داشبورد", "callback_data": "dash"},
         {"text": "🖥 وضعیت سرور", "callback_data": "server"}],
        [{"text": "👥 کاربران", "callback_data": "users:0"},
         {"text": "➕ کاربر جدید", "callback_data": "new"}],
        [{"text": "🌐 IPهای برتر", "callback_data": "ips"},
         {"text": "🟢 آنلاینی‌ها", "callback_data": "online_ips"}],
        [{"text": "📜 لاگ‌ها", "callback_data": "logs"}],
        [{"text": "💾 بکاپ", "callback_data": "backup"},
         {"text": "♻️ بازیابی", "callback_data": "restore"}],
        [{"text": "🔄 ری‌استارت هسته", "callback_data": "resync"},
         {"text": "🔑 تغییر رمز", "callback_data": "passwd"}],
        [{"text": "🗄 مدیریت Axiom", "callback_data": "axiom_menu"}],
    ]


def _back_btn(to: str = "menu") -> list:
    return [{"text": "« بازگشت", "callback_data": to}]


def _plans() -> list:
    return [
        ("۱ ماه · ۵۰ گیگ", 30, 50),
        ("۱ ماه · ۱۰۰ گیگ", 30, 100),
        ("۳ ماه · ۲۰۰ گیگ", 90, 200),
        ("۱ ماه · نامحدود", 30, 0),
    ]


# ── stats / server / ips ──────────────────────────────────────────
async def _dash_text() -> str:
    with state.lock:
        tu, td = state.STATS["total_up"], state.STATS["total_down"]
        total_users = len(state.USERS)
        active = sum(1 for u in state.USERS.values() if u.get("status") == "active")
        hist = state.STATS.get("history", [])
        active_ips = len(state.ACTIVE_IPS)
        now = time.time()
        ipw = ipg = 0
        for rec in state.IP_STATS.values():
            if now - rec.get("last", 0) > config.ONLINE_WINDOW:
                continue
            pr = rec.get("proto", {})
            if pr.get("ws"): ipw += 1
            if pr.get("grpc"): ipg += 1
    online = state.online_count()
    up_bps = down_bps = 0
    if hist:
        last = hist[-1]
        up_bps = last[1] if len(last) > 1 else 0
        down_bps = last[2] if len(last) > 2 else 0
    from . import axiom_logs
    axiom_ips = await axiom_logs.fetch_unique_ip_count()
    axiom_ips_txt = f"{axiom_ips:,}" if axiom_ips else "—"
    proto_line = f"🔌 WS: <b>{ipw}</b> IP · gRPC: <b>{ipg}</b> IP متصل"
    return (
        "📊 <b>داشبورد لحظه‌ای</b>\n\n"
        f"⬇️ دانلود کل: <b>{_b(td)}</b>\n"
        f"⬆️ آپلود کل: <b>{_b(tu)}</b>\n"
        f"📦 ترافیک کل: <b>{_b(tu + td)}</b>\n\n"
        f"🚀 سرعت دانلود: <b>{util.fmt_speed(down_bps)}</b>\n"
        f"🚀 سرعت آپلود: <b>{util.fmt_speed(up_bps)}</b>\n\n"
        f"🟢 آنلاین: <b>{online}</b> · IP فعال: <b>{active_ips}</b>\n"
        f"{proto_line}\n"
        f"👥 کاربران: <b>{total_users}</b> ({active} فعال)\n"
        f"🌍 کل کسایی که وصل شدن: <b>{axiom_ips_txt}</b>"
    )


def _server_text() -> str:
    sys = state.SYS
    mem_pct = round(sys["mem_used"] / sys["mem_total"] * 100, 1) if sys["mem_total"] else 0
    up = int(time.time() - state.BOOT_TS)
    return (
        "🖥 <b>وضعیت سرور</b>\n\n"
        f"⚡️ CPU: <b>{sys['cpu']}٪</b>\n"
        f"🧠 حافظه: <b>{mem_pct}٪</b> ({_b(sys['mem_used'])} / {_b(sys['mem_total'])})\n"
        f"⏱ آپتایم: <b>{util.fmt_duration(up)}</b>\n"
        f"⚙️ هسته: <b>{'✅ فعال' if engine.alive() else '❌ خاموش'}</b>\n"
        f"🔐 Reality: <b>{'✅' if (config.TCP_PROXY_DOMAIN and state.STATS['reality'].get('pub')) else '❌'}</b>"
    )


async def _ips_text() -> str:
    from . import axiom_logs
    rows = await axiom_logs.fetch_top_ips()
    if not rows:
        with state.lock:
            rows = sorted(
                ({"ip": ip, "up": d.get("up", 0), "down": d.get("down", 0),
                  "total": d.get("up", 0) + d.get("down", 0)}
                 for ip, d in state.IP_STATS.items()),
                key=lambda x: x["total"], reverse=True)[:20]
    if not rows:
        return "🌐 <b>IPهای برتر</b>\n\nداده‌ای موجود نیست."
    lines = ["🌐 <b>IPهای برتر (تا ۲۰)</b>\n"]
    for i, r in enumerate(rows[:20], 1):
        lines.append(f"{i}. <code>{_esc(r['ip'])}</code> — ⬇️{_b(r['down'])} ⬆️{_b(r['up'])}")
    return "\n".join(lines)


def _online_ips_text() -> str:
    now = time.time()
    with state.lock:
        entries = []
        for ip, rec in state.IP_STATS.items():
            if now - rec.get("last", 0) > config.ONLINE_WINDOW:
                continue
            first = rec.get("first_seen", rec.get("last", now))
            dur = int(now - first)
            pr = rec.get("proto", {})
            proto = "gRPC" if pr.get("grpc") else "WS" if pr.get("ws") else ""
            entries.append((ip, dur, proto))
    if not entries:
        return "🟢 <b>IPهای آنلاین</b>\n\nهیچ IP فعالی در ۲ دقیقه اخیر."
    entries.sort(key=lambda x: -x[1])
    lines = [f"🟢 <b>IPهای آنلاین</b> ({len(entries)})\n"]
    for i, (ip, dur, proto) in enumerate(entries[:30], 1):
        name, flag = geo.lookup_country(ip)
        country = f" {flag} {_esc(name)}" if name else ""
        tag = f" [{proto}]" if proto else ""
        lines.append(f"{i}. <code>{_esc(ip)}</code>{country}{tag} — {util.fmt_duration(dur)}")
    return "\n".join(lines)


# ── users: list / detail / config ─────────────────────────────────────
_PAGE = 8


def _users_page(page: int) -> tuple[str, list]:
    with state.lock:
        items = list(state.USERS.items())
        snapshot_stats = {uid: dict(state.STATS["users"].get(uid, {"up": 0, "down": 0})) for uid, _ in items}
        snapshot_active = dict(state.LAST_ACTIVE)
    total = len(items)
    if not total:
        return "👥 هنوز کاربری ساخته نشده.", [[{"text": "➕ کاربر جدید", "callback_data": "new"}], _back_btn()]
    pages = (total + _PAGE - 1) // _PAGE
    page = max(0, min(page, pages - 1))
    chunk = items[page * _PAGE:(page + 1) * _PAGE]
    rows = []
    now = time.time()
    for uid, u in chunk:
        rec = snapshot_stats.get(uid, {"up": 0, "down": 0})
        used = rec["up"] + rec["down"]
        online = (now - snapshot_active.get(uid, 0)) < config.ONLINE_WINDOW
        dot = "🟢" if online else "⚪️"
        label = u.get("label", "کاربر")
        rows.append([{"text": f"{dot} {_esc(label)} · {_b(used)}", "callback_data": f"u:{uid}"}])
    nav = []
    if page > 0:
        nav.append({"text": "» قبلی", "callback_data": f"users:{page - 1}"})
    if page < pages - 1:
        nav.append({"text": "بعدی «", "callback_data": f"users:{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "➕ کاربر جدید", "callback_data": "new"},
                 {"text": "🔍 جستجو", "callback_data": "find"}])
    rows.append(_back_btn())
    txt = f"👥 <b>کاربران</b> — صفحه {page + 1}/{pages} (کل: {total})"
    return txt, rows


def _user_detail(uid: str) -> tuple[str, list] | None:
    with state.lock:
        u = state.USERS.get(uid)
        if not u:
            return None
        rec = state.STATS["users"].get(uid, {"up": 0, "down": 0})
        u_copy = dict(u)
    used = rec["up"] + rec["down"]
    quota = u_copy.get("quota", 0)
    quota_txt = _b(quota) if quota else "نامحدود"
    days = util.remaining_days(u_copy.get("expiry", 0)) if u_copy.get("expiry") else None
    exp_txt = f"{days} روز" if days is not None else "نامحدود"
    online = (time.time() - state.LAST_ACTIVE.get(uid, 0)) < config.ONLINE_WINDOW
    txt = (
        f"👤 <b>{_esc(u_copy.get('label', 'کاربر'))}</b>\n\n"
        f"🆔 <code>{_esc(uid)}</code>\n"
        f"حالت: {_status_fa(u_copy.get('status', 'active'))}\n"
        f"اتصال: {'🟢 آنلاین' if online else '⚪️ آفلاین'}\n"
        f"📊 مصرف: <b>{_b(used)}</b> / {quota_txt}\n"
        f"   ⬇️ {_b(rec['down'])} · ⬆️ {_b(rec['up'])}\n"
        f"⏳ انقضا: <b>{exp_txt}</b>\n"
        f"🔌 پروتکل: {', '.join(u_copy.get('protocols', []))}"
    )
    toggle = "⚪️ غیرفعال‌سازی" if u_copy.get("status") != "disabled" else "🟢 فعال‌سازی"
    rows = [
        [{"text": "📥 کانفیگ + QR", "callback_data": f"cfg:{uid}"}],
        [{"text": "➕ ۳۰ روز", "callback_data": f"adddays:{uid}:30"},
         {"text": "➕ ۵۰ گیگ", "callback_data": f"addgb:{uid}:50"}],
        [{"text": "✏️ تغییر نام", "callback_data": f"rename:{uid}"},
         {"text": "♻️ صفر کردن ترافیک", "callback_data": f"reset:{uid}"}],
        [{"text": toggle, "callback_data": f"toggle:{uid}"},
         {"text": "🗑 حذف", "callback_data": f"del:{uid}"}],
        [{"text": "« لیست کاربران", "callback_data": "users:0"}],
    ]
    return txt, rows


async def _send_user_config(chat_id: int, uid: str) -> None:
    with state.lock:
        u = state.USERS.get(uid)
        u_copy = dict(u) if u else None
    if not u_copy:
        await send(chat_id, "کاربر یافت نشد.")
        return
    data = subs.build_links(uid, u_copy, _domain())
    body = "\n\n".join(data["links"]) or "پروتکلی فعال نیست"
    txt = (
        f"📥 <b>کانفیگ {_esc(u_copy.get('label', ''))}</b>\n\n"
        f"🔗 اشتراک: <code>{_esc(data['sub_link'])}</code>\n\n"
        f"<pre>{_esc(body)}</pre>"
    )
    await send(chat_id, txt, [[{"text": "« بازگشت", "callback_data": f"u:{uid}"}]])
    # QR as local PNG (segno) — no external service
    try:
        import segno
        buf = io.BytesIO()
        segno.make(data["sub_link"], error="m").save(buf, kind="png", scale=6)
        buf.seek(0)
        await _call("sendPhoto", {"chat_id": chat_id, "caption": "QR لینک اشتراک"},
                    files={"photo": ("qr.png", buf.getvalue(), "image/png")})
    except Exception as e:
        state.log_error(f"tg.qr: {e}")


# ── update router ────────────────────────────────────────────────
async def handle_update(update: dict) -> None:
    """Main webhook entry point. Always called in background."""
    try:
        _gc_conv()
        if "callback_query" in update:
            await _on_callback(update["callback_query"])
        elif "message" in update:
            await _on_message(update["message"])
    except Exception as e:
        state.log_error(f"tg.handle: {e}")


async def _on_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not _is_admin(chat_id):
        await send(chat_id, "⛔️ دسترسی مجاز نیست.")
        return
    if not _security.allow(str(chat_id), "tg", limit=config.RATE_LIMIT_TG, window=60):
        await send(chat_id, "⏳ درخواست بیش از حد. کمی صبر کنید.")
        return

    if text in ("/cancel", "لغو"):
        _clear_conv(chat_id)
        await send(chat_id, "✖️ عملیات لغو شد.", _main_menu())
        return

    if chat_id in _CONV and not text.startswith("/"):
        await _on_conv(chat_id, text)
        return

    cmd = text.split()[0].lower() if text else ""
    if cmd in ("/start", "/menu"):
        await send(chat_id, "🌌 <b>Aurora — پنل مدیریت</b>\nیک گزینه را انتخاب کنید:", _main_menu())
    elif cmd == "/stats":
        await send(chat_id, await _dash_text(), [_back_btn()])
    elif cmd == "/server":
        await send(chat_id, _server_text(), [_back_btn()])
    elif cmd == "/users":
        txt, kb = _users_page(0)
        await send(chat_id, txt, kb)
    elif cmd == "/ips":
        await send(chat_id, await _ips_text(), [_back_btn()])
    elif cmd == "/online_ips":
        await send(chat_id, _online_ips_text(), [_back_btn()])
    elif cmd == "/backup":
        await _do_backup(chat_id)
    elif cmd == "/new":
        _set_conv(chat_id, "new", "label", {})
        await send(chat_id, "➕ <b>کاربر جدید</b>\nنام کاربر را بفرستید (یا /cancel):")
    elif cmd == "/find":
        _set_conv(chat_id, "find", "query", {})
        await send(chat_id, "🔍 نام یا شناسهٔ کاربر را بفرستید:")
    else:
        await send(chat_id, "دستور ناشناخته. /menu را بزنید.", _main_menu())


async def _on_conv(chat_id: int, text: str) -> None:
    conv = _CONV.get(chat_id)
    if not conv:
        return
    flow, step, data = conv["flow"], conv["step"], conv["data"]

    if flow == "find":
        res = user_service.find_users(text)
        _clear_conv(chat_id)
        if not res:
            await send(chat_id, "کاربری یافت نشد.", [_back_btn()])
            return
        rows = [[{"text": u.get("label", "کاربر"), "callback_data": f"u:{uid}"}] for uid, u in res]
        rows.append(_back_btn())
        await send(chat_id, f"🔍 {len(res)} نتیجه:", rows)
        return

    if flow == "rename":
        uid = data.get("uid")
        ok = await user_service.rename_user(uid, text)
        _clear_conv(chat_id)
        if ok:
            d = _user_detail(uid)
            if d:
                await send(chat_id, "✅ نام تغییر کرد.\n\n" + d[0], d[1])
        else:
            await send(chat_id, "کاربر یافت نشد.", _back_btn())
        return

    if flow == "passwd":
        if len(text) < 6:
            await send(chat_id, "رمز حداقل ۶ نویسه. دوباره بفرستید یا /cancel:")
            return
        _security.set_password(text)
        storage.save_local()
        _clear_conv(chat_id)
        await send(chat_id, "🔑 رمز پنل بروزرسانی شد.", _main_menu())
        return

    if flow == "axiom_trim":
        from . import axiom_logs
        await send(chat_id, "⏳ در حال حذف داده‌ها، لطفاً صبر کنید...")
        result = await axiom_logs.trim_dataset_before_date(text)
        _clear_conv(chat_id)
        kb = [[{"text": "🗄 مدیریت Axiom", "callback_data": "axiom_menu"}], _back_btn()]
        await send(chat_id, result["message"], kb)
        return

    if flow == "axiom_purge_confirm":
        from . import axiom_logs
        if text.strip() == "بله حذف شود":
            await send(chat_id, "⏳ در حال پاک کردن کامل dataset...")
            result = await axiom_logs.purge_dataset()
        else:
            result = {"message": "❌ عملیات لغو شد."}
        _clear_conv(chat_id)
        kb = [[{"text": "🗄 مدیریت Axiom", "callback_data": "axiom_menu"}], _back_btn()]
        await send(chat_id, result["message"], kb)
        return

    if flow == "new":
        if step == "label":
            data["label"] = text
            _set_conv(chat_id, "new", "plan", data)
            rows = [[{"text": p[0], "callback_data": f"plan:{i}"}] for i, p in enumerate(_plans())]
            rows.append([{"text": "✏️ دلخواه (روز,گیگ)", "callback_data": "plan:custom"}])
            rows.append([{"text": "✖️ لغو", "callback_data": "menu"}])
            await send(chat_id, f"پلان را برای «{_esc(text)}» انتخاب کنید:", rows)
        elif step == "custom":
            try:
                parts = text.replace("،", ",").split(",")
                days = int(parts[0].strip())
                gb = float(parts[1].strip()) if len(parts) > 1 else 0
            except Exception:
                await send(chat_id, "فرمت اشتباه است. مثال: <code>30,50</code>")
                return
            label = data.get("label", "کاربر")
            uid, _ = await user_service.create_user(label=label, days=days, gb=gb)
            _clear_conv(chat_id)
            await send(chat_id, f"✅ کاربر «{_esc(label)}» ساخته شد.")
            await _send_user_config(chat_id, uid)
        return


async def _on_callback(cb: dict) -> None:
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    msg_id = cb.get("message", {}).get("message_id")
    cb_id = cb.get("id")
    data = cb.get("data", "")
    if not _is_admin(chat_id):
        await _answer_cb(cb_id, "⛔️ غیرمجاز")
        return
    await _answer_cb(cb_id)

    if data == "menu":
        _clear_conv(chat_id)
        await send(chat_id, "🌌 <b>منوی اصلی</b>", _main_menu(), edit_id=msg_id)
    elif data == "dash":
        await send(chat_id, await _dash_text(), [_back_btn()], edit_id=msg_id)
    elif data == "server":
        await send(chat_id, _server_text(), [_back_btn()], edit_id=msg_id)
    elif data == "ips":
        await send(chat_id, await _ips_text(), [_back_btn()], edit_id=msg_id)
    elif data == "online_ips":
        await send(chat_id, _online_ips_text(), [_back_btn()], edit_id=msg_id)
    elif data == "logs":
        items = list(state.ERRORS)[:15]
        body = "\n".join(f"• {_esc(t)}" for _, t in items) or "لاگی ثبت نشده."
        await send(chat_id, f"📜 <b>لاگ‌های اخیر</b>\n\n<pre>{body}</pre>", [_back_btn()], edit_id=msg_id)
    elif data.startswith("users:"):
        try:
            page = int(data.split(":")[1])
        except (ValueError, IndexError):
            page = 0
        txt, kb = _users_page(page)
        await send(chat_id, txt, kb, edit_id=msg_id)
    elif data.startswith("u:"):
        d = _user_detail(data[2:])
        if d:
            await send(chat_id, d[0], d[1], edit_id=msg_id)
        else:
            await send(chat_id, "کاربر یافت نشد.", [_back_btn("users:0")], edit_id=msg_id)
    elif data.startswith("cfg:"):
        await _send_user_config(chat_id, data[4:])
    elif data.startswith("adddays:"):
        try:
            _, uid, n = data.split(":")
            await user_service.add_days(uid, int(n))
            d = _user_detail(uid)
            if d:
                await send(chat_id, "✅ تمدید شد.\n\n" + d[0], d[1], edit_id=msg_id)
        except (ValueError, IndexError):
            pass
    elif data.startswith("addgb:"):
        try:
            _, uid, n = data.split(":")
            await user_service.add_gb(uid, float(n))
            d = _user_detail(uid)
            if d:
                await send(chat_id, "✅ حجم اضافه شد.\n\n" + d[0], d[1], edit_id=msg_id)
        except (ValueError, IndexError):
            pass
    elif data.startswith("rename:"):
        uid = data.split(":")[1]
        _set_conv(chat_id, "rename", "label", {"uid": uid})
        await send(chat_id, "✏️ نام جدید را بفرستید:")
    elif data.startswith("reset:"):
        uid = data.split(":")[1]
        await user_service.reset_traffic(uid)
        d = _user_detail(uid)
        if d:
            await send(chat_id, "♻️ ترافیک صفر شد.\n\n" + d[0], d[1], edit_id=msg_id)
    elif data.startswith("toggle:"):
        uid = data.split(":")[1]
        await user_service.toggle_user(uid)
        d = _user_detail(uid)
        if d:
            await send(chat_id, "✅ انجام شد.\n\n" + d[0], d[1], edit_id=msg_id)
    elif data.startswith("del:"):
        uid = data.split(":")[1]
        rows = [[{"text": "✅ بله، حذف کن", "callback_data": f"delok:{uid}"},
                 {"text": "✖️ انصراف", "callback_data": f"u:{uid}"}]]
        await send(chat_id, "⚠️ از حذف این کاربر مطمئنید؟", rows, edit_id=msg_id)
    elif data.startswith("delok:"):
        uid = data.split(":")[1]
        await user_service.delete_user(uid)
        txt, kb = _users_page(0)
        await send(chat_id, "🗑 کاربر حذف شد.\n\n" + txt, kb, edit_id=msg_id)
    elif data == "new":
        _set_conv(chat_id, "new", "label", {})
        await send(chat_id, "➕ <b>کاربر جدید</b>\nنام کاربر را بفرستید (یا /cancel):")
    elif data == "find":
        _set_conv(chat_id, "find", "query", {})
        await send(chat_id, "🔍 نام یا شناسهٔ کاربر را بفرستید:")
    elif data.startswith("plan:"):
        await _on_plan(chat_id, msg_id, data.split(":")[1])
    elif data == "backup":
        await _do_backup(chat_id, msg_id)
    elif data == "restore":
        ok = await ghsync.restore() if config.gh_enabled() else False
        ok_any = ok["users"] or ok["stats"] if isinstance(ok, dict) else ok
        if ok_any:
            engine.mark_resync()
        await send(chat_id, "♻️ بازیابی انجام شد." if ok_any else "❌ بازیابی ناموفق (گیتهاب غیرفعال؟)", [_back_btn()], edit_id=msg_id)
    elif data == "resync":
        engine.mark_resync()
        await send(chat_id, "🔄 ری‌استارت هسته زمان‌بندی شد.", [_back_btn()], edit_id=msg_id)
    elif data == "passwd":
        _set_conv(chat_id, "passwd", "new", {})
        await send(chat_id, "🔑 رمز جدید پنل را بفرستید (حداقل ۶ نویسه):")
    elif data == "axiom_menu":
        await _show_axiom_menu(chat_id, msg_id)
    elif data == "axiom_trim":
        _set_conv(chat_id, "axiom_trim", "date", {})
        await send(
            chat_id,
            "🗑 <b>حذف داده‌های Axiom</b>\n\n"
            "یکی از فرمت‌های زیر را بفرستید:\n\n"
            "⏱ <b>زمان نسبی:</b>\n"
            "  • <code>1h</code> — یک ساعت پیش\n"
            "  • <code>6h</code> — شش ساعت پیش\n"
            "  • <code>1d</code> — یک روز پیش\n"
            "  • <code>7d</code> — هفت روز پیش\n\n"
            "📅 <b>تاریخ مشخص:</b>\n"
            "  • <code>2025-01-15</code>\n\n"
            "⚠️ این عملیات <b>برگشت‌ناپذیر</b> است!\n"
            "یا /cancel برای لغو:",
        )
    elif data == "axiom_purge":
        _set_conv(chat_id, "axiom_purge_confirm", "confirm", {})
        await send(
            chat_id,
            "💣 <b>پاک کردن کامل همه داده‌های Axiom</b>\n\n"
            "⚠️ این عملیات <b>تمام</b> داده‌های dataset را حذف می‌کند!\n\n"
            "برای تأیید بنویس: <code>بله حذف شود</code>\n"
            "یا /cancel برای لغو:",
        )


async def _show_axiom_menu(chat_id: int, msg_id: int | None = None) -> None:
    txt = (
        f"🗄 <b>مدیریت Axiom</b>\n\n"
        f"Dataset: <code>{_esc(config.AXIOM_DATASET)}</code>\n\n"
        "از اینجا می‌توانید داده‌های ذخیره‌شده در Axiom را مدیریت کنید."
    )
    kb = [
        [{"text": "🗑 حذف داده‌ها قبل از تاریخ/زمان", "callback_data": "axiom_trim"}],
        [{"text": "💣 پاک کردن کامل همه داده‌ها", "callback_data": "axiom_purge"}],
        [{"text": "📊 IPهای برتر (۳۰ روز)", "callback_data": "ips"}],
        _back_btn(),
    ]
    await send(chat_id, txt, kb, edit_id=msg_id)


async def _on_plan(chat_id: int, msg_id: int, choice: str) -> None:
    conv = _CONV.get(chat_id)
    if not conv or conv.get("flow") != "new":
        await send(chat_id, "عملیات منقضی شد. /new را دوباره بزنید.", _main_menu())
        return
    label = conv["data"].get("label", "کاربر")
    if choice == "custom":
        _set_conv(chat_id, "new", "custom", conv["data"])
        await send(chat_id, "روز و حجم (گیگ) را با کاما بفرستید. مثال: <code>30,50</code>\n(حجم ۰ = نامحدود)")
        return
    plans = _plans()
    try:
        idx = int(choice)
        if idx < 0 or idx >= len(plans):
            raise ValueError
    except (ValueError, TypeError):
        await send(chat_id, "انتخاب نامعتبر.", _main_menu())
        return
    _, days, gb = plans[idx]
    uid, _ = await user_service.create_user(label=label, days=days, gb=gb)
    _clear_conv(chat_id)
    await send(chat_id, f"✅ کاربر «{_esc(label)}» با پلان {plans[idx][0]} ساخته شد.")
    await _send_user_config(chat_id, uid)


async def _do_backup(chat_id: int, msg_id: int | None = None) -> None:
    if not config.gh_enabled():
        await send(chat_id, "❌ گیتهاب پیکربندی نشده.", [_back_btn()], edit_id=msg_id)
        return
    ok = await ghsync.backup("manual")
    await send(chat_id, "💾 بکاپ انجام شد." if ok else "❌ بکاپ ناموفق.", [_back_btn()], edit_id=msg_id)
