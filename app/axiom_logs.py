"""Axiom log shipping + querying, with TTL caching.

v3 changes:
  - fetch_top_ips() and fetch_unique_ip_count() are cached (60s TTL).
    Previously every dashboard refresh fired a 5-year-range APL query.
  - Uses shared ReusableClient (no duplicated _get_client).
  - Stale-cache fallback: on error, return last good value.
  - Field-aware parsing (already good in v2, preserved).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import config, state
from .http_util import axiom_client

AXIOM_API_URL = "https://api.axiom.co"

# ── caches ───────────────────────────────────────────────────────────
_top_ips_cache: dict = {"data": [], "ts": 0.0}
_unique_count_cache: dict = {"value": 0, "ts": 0.0}


async def ensure_dataset() -> bool:
    """Create the Axiom dataset if it doesn't exist."""
    if not config.AXIOM_TOKEN:
        state.log_error("Axiom: AXIOM_TOKEN is EMPTY! Logging disabled.")
        return False

    headers = {
        "Authorization": f"Bearer {config.AXIOM_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        client = await axiom_client.get()
        r = await client.get(
            f"{AXIOM_API_URL}/v1/datasets/{config.AXIOM_DATASET}",
            headers=headers,
        )
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            state.log_error("Axiom: Dataset not found. Creating it...")
            r_create = await client.post(
                f"{AXIOM_API_URL}/v1/datasets",
                headers=headers,
                json={
                    "name": config.AXIOM_DATASET,
                    "description": "Aurora Logs v3",
                },
            )
            if r_create.status_code in (200, 201):
                return True
            state.log_error(
                f"Axiom create failed: HTTP {r_create.status_code}"
            )
        return False
    except Exception as e:
        state.log_error(f"Axiom Ensure Exception: {e}")
        return False


async def send_to_axiom(logs: list, event_type: str = "log") -> None:
    """Ship logs/events to Axiom. Fire-and-forget by caller."""
    if not logs or not config.AXIOM_TOKEN:
        return

    payload: list = []
    for item in logs:
        if isinstance(item, str):
            payload.append(
                {
                    "message": item,
                    "source": "xray-core",
                    "event_type": event_type,
                }
            )
        elif isinstance(item, dict):
            item["source"] = "aurora-tracker"
            item["event_type"] = event_type
            payload.append(item)

    try:
        client = await axiom_client.get()
        r = await client.post(
            f"{AXIOM_API_URL}/v1/datasets/{config.AXIOM_DATASET}/ingest",
            headers={
                "Authorization": f"Bearer {config.AXIOM_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=5,
        )
        if r.status_code not in (200, 202):
            state.log_error(
                f"Axiom Ingest Failed: HTTP {r.status_code} - {r.text[:150]}"
            )
    except Exception as e:
        state.log_error(f"Axiom Send Exception: {e}")




async def purge_dataset() -> dict:
    """حذف کامل dataset و ساخت مجدد آن — همه داده‌ها پاک می‌شوند."""
    if not config.AXIOM_TOKEN:
        return {"ok": False, "message": "AXIOM_TOKEN تنظیم نشده."}

    headers = {
        "Authorization": f"Bearer {config.AXIOM_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        client = await axiom_client.get()
        # مرحله ۱: حذف dataset
        r_del = await client.delete(
            f"{AXIOM_API_URL}/v1/datasets/{config.AXIOM_DATASET}",
            headers=headers,
        )
        if r_del.status_code not in (200, 204, 404):
            err = r_del.text[:200]
            state.log_error(f"Axiom Delete Failed: HTTP {r_del.status_code} - {err}")
            return {
                "ok": False,
                "message": f"❌ خطا در حذف dataset: HTTP {r_del.status_code}\n<pre>{err}</pre>",
            }
        # مرحله ۲: ساخت مجدد
        r_create = await client.post(
            f"{AXIOM_API_URL}/v1/datasets",
            headers=headers,
            json={
                "name": config.AXIOM_DATASET,
                "description": "Aurora Logs v3",
            },
        )
        if r_create.status_code in (200, 201):
            # پاک کردن کش‌های local
            _top_ips_cache["data"] = []
            _top_ips_cache["ts"] = 0.0
            _unique_count_cache["value"] = 0
            _unique_count_cache["ts"] = 0.0
            return {
                "ok": True,
                "message": (
                    f"✅ dataset <code>{config.AXIOM_DATASET}</code> کاملاً پاک و دوباره ساخته شد."
                ),
            }
        err = r_create.text[:200]
        state.log_error(f"Axiom Recreate Failed: HTTP {r_create.status_code} - {err}")
        return {
            "ok": False,
            "message": f"❌ dataset حذف شد ولی ساخت مجدد ناموفق بود: HTTP {r_create.status_code}",
        }
    except Exception as e:
        state.log_error(f"Axiom Purge Exception: {e}")
        return {"ok": False, "message": f"❌ خطای شبکه: {e}"}

async def _raw_fetch_top_ips() -> list:
    apl_query = (
        f"['{config.AXIOM_DATASET}']"
        f" | where isnotnull(client_ip)"
        f" | summarize TotalUp = sum(up_bytes), TotalDown = sum(down_bytes) by client_ip"
        f" | order by TotalDown desc"
        f" | take 20"
    )
    now = datetime.now(timezone.utc)
    payload = {
        "apl": apl_query,
        "startTime": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        client = await axiom_client.get()
        r = await client.post(
            f"{AXIOM_API_URL}/v1/datasets/_apl",
            headers={
                "Authorization": f"Bearer {config.AXIOM_TOKEN}",
                "Content-Type": "application/json",
            },
            params={"format": "tabular-rows"},
            json=payload,
        )
        if r.status_code != 200:
            state.log_error(
                f"Axiom Query Failed: HTTP {r.status_code} - {r.text[:150]}"
            )
            return []
        return _parse_top_ips(r.json())
    except Exception as e:
        state.log_error(f"Axiom Fetch Exception: {e}")
        return []


async def fetch_top_ips() -> list:
    """Cached fetch_top_ips (60s TTL). Returns stale data on error."""
    if not config.AXIOM_TOKEN:
        return []
    now = time.time()
    if now - _top_ips_cache["ts"] < config.AXIOM_CACHE_TTL:
        return _top_ips_cache["data"]
    data = await _raw_fetch_top_ips()
    if data:
        _top_ips_cache["data"] = data
        _top_ips_cache["ts"] = now
    return _top_ips_cache["data"]


def _parse_top_ips(data: dict) -> list:
    """Field-aware parsing of Axiom tabular-rows response."""
    result: list = []
    if not isinstance(data, dict) or "tables" not in data:
        return result
    tables = data.get("tables", [])
    if not tables:
        return result
    first_table = tables[0]
    fields = first_table.get("fields", [])
    raw_cols = first_table.get("columns", [])
    if not raw_cols or not fields:
        return result
    field_names = [
        (f.get("name", "") if isinstance(f, dict) else str(f)) for f in fields
    ]
    try:
        ip_idx = field_names.index("client_ip")
        up_idx = field_names.index("TotalUp")
        dn_idx = field_names.index("TotalDown")
    except ValueError:
        state.log_error(
            f"Axiom: unexpected fields in response: {field_names}"
        )
        return result
    if not isinstance(raw_cols[0], list):
        return result
    rows = list(map(list, zip(*raw_cols)))
    max_idx = max(ip_idx, up_idx, dn_idx)
    for row in rows:
        if len(row) <= max_idx:
            continue
        ip_val = row[ip_idx]
        if not ip_val or str(ip_val).strip() == "":
            continue
        ip = str(ip_val)
        u = int(row[up_idx]) if isinstance(row[up_idx], (int, float)) else 0
        d = int(row[dn_idx]) if isinstance(row[dn_idx], (int, float)) else 0
        result.append({"ip": ip, "up": u, "down": d, "total": u + d})
    return result


async def trim_dataset_before_date(before_date_str: str) -> dict:
    """Delete all Axiom dataset entries before a given date or relative time.

    Accepts:
      - Relative:  1h, 2h, 24h, 3d, 7d, 30d  (hours or days ago)
      - Date:      2025-01-15  or  2025-01-15T10:00:00
    """
    if not config.AXIOM_TOKEN:
        return {"ok": False, "message": "AXIOM_TOKEN تنظیم نشده."}

    import re as _re
    raw = before_date_str.strip().lower()
    now_utc = datetime.now(timezone.utc)
    display_label = before_date_str.strip()

    m = _re.fullmatch(r"(\d+)(h|d)", raw)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delta_hours = amount if unit == "h" else amount * 24
        display_label = f"{amount} ساعت پیش" if unit == "h" else f"{amount} روز پیش"
    else:
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(before_date_str.strip(), fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return {
                "ok": False,
                "message": (
                    "فرمت نادرست. مثال‌های صحیح:\n"
                    "<code>1h</code> — یک ساعت پیش\n"
                    "<code>3d</code> — سه روز پیش\n"
                    "<code>2025-01-15</code> — تاریخ مشخص"
                ),
            }
        dt = dt.replace(tzinfo=timezone.utc)
        delta_hours = int((now_utc - dt).total_seconds() / 3600)

    if delta_hours <= 0:
        return {"ok": False, "message": "زمان وارد شده باید در گذشته باشد."}

    max_duration = f"{delta_hours}h"
    payload = {"maxDuration": max_duration}
    headers = {
        "Authorization": f"Bearer {config.AXIOM_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        client = await axiom_client.get()
        r = await client.post(
            f"{AXIOM_API_URL}/v1/datasets/{config.AXIOM_DATASET}/trim",
            headers=headers,
            json=payload,
        )
        if r.status_code in (200, 202, 204):
            return {
                "ok": True,
                "message": (
                    f"✅ داده‌های قبل از <b>{display_label}</b> "
                    f"از dataset <code>{config.AXIOM_DATASET}</code> حذف شدند."
                ),
            }
        err = r.text[:200]
        state.log_error(f"Axiom Trim Failed: HTTP {r.status_code} - {err}")
        return {
            "ok": False,
            "message": (
                f"❌ خطا از Axiom: HTTP {r.status_code}\n<pre>{err}</pre>"
            ),
        }
    except Exception as e:
        state.log_error(f"Axiom Trim Exception: {e}")
        return {"ok": False, "message": f"❌ خطای شبکه: {e}"}
    payload = {"maxDuration": max_duration}
    headers = {
        "Authorization": f"Bearer {config.AXIOM_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        client = await axiom_client.get()
        r = await client.post(
            f"{AXIOM_API_URL}/v1/datasets/{config.AXIOM_DATASET}/trim",
            headers=headers,
            json=payload,
        )
        if r.status_code in (200, 202, 204):
            return {
                "ok": True,
                "message": (
                    f"✅ داده‌های قبل از <b>{before_date_str}</b> "
                    f"از dataset <code>{config.AXIOM_DATASET}</code> حذف شدند."
                ),
            }
        err = r.text[:200]
        state.log_error(f"Axiom Trim Failed: HTTP {r.status_code} - {err}")
        return {
            "ok": False,
            "message": (
                f"❌ خطا از Axiom: HTTP {r.status_code}\n<pre>{err}</pre>"
            ),
        }
    except Exception as e:
        state.log_error(f"Axiom Trim Exception: {e}")
        return {"ok": False, "message": f"❌ خطای شبکه: {e}"}


async def _raw_fetch_unique_ip_count() -> int:
    apl_query = (
        f"['{config.AXIOM_DATASET}']"
        f" | where event_type == 'ip_traffic'"
        f" | where isnotnull(client_ip)"
        f" | summarize count = dcount(client_ip)"
    )
    now = datetime.now(timezone.utc)
    payload = {
        "apl": apl_query,
        "startTime": "2020-01-01T00:00:00Z",
        "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        client = await axiom_client.get()
        r = await client.post(
            f"{AXIOM_API_URL}/v1/datasets/_apl",
            headers={
                "Authorization": f"Bearer {config.AXIOM_TOKEN}",
                "Content-Type": "application/json",
            },
            params={"format": "tabular-rows"},
            json=payload,
        )
        if r.status_code != 200:
            state.log_error(f"Axiom IP Count Failed: HTTP {r.status_code}")
            return 0
        data = r.json()
        tables = data.get("tables", [])
        if not tables:
            return 0
        first = tables[0]
        fields = first.get("fields", [])
        cols = first.get("columns", [])
        if not fields or not cols:
            return 0
        field_names = [
            (f.get("name", "") if isinstance(f, dict) else str(f))
            for f in fields
        ]
        try:
            idx = field_names.index("count")
        except ValueError:
            return 0
        col = cols[idx]
        if not col:
            return 0
        val = col[0]
        return int(val) if isinstance(val, (int, float)) else 0
    except Exception as e:
        state.log_error(f"Axiom IP Count Exception: {e}")
        return 0


async def fetch_unique_ip_count() -> int:
    """Cached fetch_unique_ip_count (60s TTL). Returns stale data on error."""
    if not config.AXIOM_TOKEN:
        return 0
    now = time.time()
    if now - _unique_count_cache["ts"] < config.AXIOM_CACHE_TTL:
        return _unique_count_cache["value"]
    v = await _raw_fetch_unique_ip_count()
    if v > 0 or _unique_count_cache["ts"] == 0.0:
        _unique_count_cache["value"] = v
        _unique_count_cache["ts"] = now
    return _unique_count_cache["value"]
