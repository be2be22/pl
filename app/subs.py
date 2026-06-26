"""Subscription link + Clash config generation.

v3 fix: Reality link filtering now uses URL substring ("security=reality")
instead of label substring ("Reality"), which previously never matched
because Reality links don't contain the capitalized word "Reality".
"""
from __future__ import annotations

import base64
import json
import urllib.parse

from . import config, state


def _reality_sni(user_sni: str = "") -> str:
    return user_sni or config.REALITY_DEST.split(":")[0]


def _encode_label(label: str) -> str:
    """URL-encode label for fragment (handles Persian/special chars)."""
    return urllib.parse.quote(label, safe="")


def ws_link(uid: str, server: str, domain: str, label: str) -> str:
    return (
        f"vless://{uid}@{server}:443?encryption=none&security=tls&type=ws"
        f"&host={domain}&path=/ws&sni={domain}&fp=chrome#{_encode_label(label)}"
    )


def grpc_link(uid: str, server: str, domain: str, label: str) -> str:
    """VLESS + gRPC + TLS link.

    Key advantages over WS:
      - type=grpc: HTTP/2 multiplexed streams (no per-message framing)
      - serviceName: gRPC service path (must match server's grpcSettings)
      - mode=gun: standard gRPC mode (most compatible)
      - Lower latency on mobile networks with packet loss
    """
    return (
        f"vless://{uid}@{server}:443?encryption=none&security=tls&type=grpc"
        f"&serviceName={config.GRPC_PATH.lstrip('/')}&mode=gun"
        f"&sni={domain}&fp=chrome#{_encode_label(label)}"
    )


def reality_link(uid: str, label: str, sni: str = "",
                 server: str = "", port: str = "") -> str:
    """VLESS + Reality link.

    v3.4: accepts custom server and port for extra Reality ports (e.g. 2083).
    If not provided, uses TCP_PROXY_DOMAIN and TCP_PROXY_PORT from config.
    """
    r = state.STATS["reality"]
    srv = server or config.TCP_PROXY_DOMAIN
    prt = port or config.TCP_PROXY_PORT
    if not (srv and r.get("pub")):
        return ""
    final_sni = _reality_sni(sni)
    return (
        f"vless://{uid}@{srv}:{prt}"
        f"?encryption=none&security=reality&sni={final_sni}&fp=chrome"
        f"&pbk={r['pub']}&sid={r['sid']}&type=tcp&flow=xtls-rprx-vision"
        f"#{_encode_label(label)}"
    )


def build_links(uid: str, user: dict, domain: str) -> dict:
    label = user.get("label", "user")
    protos = user.get("protocols", ["ws", "grpc", "reality"])
    links: list[str] = []

    if "ws" in protos:
        links.append(ws_link(uid, domain, domain, f"{label}-WS-Main"))
        extra_ips = user.get("ws_ips", "")
        if extra_ips:
            for ip in extra_ips.split(","):
                ip = ip.strip()
                if ip:
                    links.append(ws_link(uid, ip, domain, f"{label}-WS-{ip}"))

    if "grpc" in protos:
        # gRPC link — same server/domain as WS (nginx routes by path)
        links.append(grpc_link(uid, domain, domain, f"{label}-gRPC"))

    if "reality" in protos:
        user_sni = user.get("reality_sni", "")
        # Main Reality link (port 443 or TCP_PROXY_PORT)
        link = reality_link(uid, label, user_sni)
        if link:
            links.append(link)
        # v3.4: Extra Reality links on alternate ports (e.g. 2083)
        # These use the same server/domain but different port.
        # User must set RAILWAY_TCP_PROXY_DOMAIN for these to work.
        for extra_port in config.EXTRA_REALITY_PORTS:
            extra_link = reality_link(
                uid, f"{label}-Port{extra_port}", user_sni,
                port=str(extra_port),
            )
            if extra_link:
                links.append(extra_link)

    sub_b64 = base64.b64encode("\n".join(links).encode()).decode()

    # Filter by URL content, not label substring (v3 fix)
    ws_links = [l for l in links if "type=ws" in l]
    grpc_links = [l for l in links if "type=grpc" in l]
    reality_links = [l for l in links if "security=reality" in l]

    return {
        "ws": ws_links,
        "grpc": grpc_links,
        "reality": reality_links,
        "sub_link": f"https://{domain}/s/{user.get('sid')}",
        "sub_b64": sub_b64,
        "links": links,
    }


def clash_config(uid: str, user: dict, domain: str) -> str:
    label = user.get("label", "user")
    protos = user.get("protocols", ["ws", "grpc", "reality"])
    proxies: list = []
    names: list = []

    if "ws" in protos:
        names.append(f"{label}-WS")
        proxies.append({
            "name": f"{label}-WS",
            "type": "vless",
            "server": domain,
            "port": 443,
            "uuid": uid,
            "udp": True,
            "tls": True,
            "servername": domain,
            "network": "ws",
            "ws-opts": {"path": "/ws", "headers": {"Host": domain}},
            "client-fingerprint": "chrome",
        })

    if "grpc" in protos:
        names.append(f"{label}-gRPC")
        proxies.append({
            "name": f"{label}-gRPC",
            "type": "vless",
            "server": domain,
            "port": 443,
            "uuid": uid,
            "udp": True,
            "tls": True,
            "servername": domain,
            "network": "grpc",
            "grpc-opts": {
                "grpc-service-name": config.GRPC_PATH.lstrip("/"),
            },
            "client-fingerprint": "chrome",
        })

    r = state.STATS["reality"]
    if "reality" in protos and config.TCP_PROXY_DOMAIN and r.get("pub"):
        names.append(f"{label}-Reality")
        proxies.append({
            "name": f"{label}-Reality",
            "type": "vless",
            "server": config.TCP_PROXY_DOMAIN,
            "port": int(config.TCP_PROXY_PORT or 443),
            "uuid": uid,
            "udp": True,
            "tls": True,
            "flow": "xtls-rprx-vision",
            "servername": _reality_sni(user.get("reality_sni", "")),
            "network": "tcp",
            "reality-opts": {"public-key": r["pub"], "short-id": r["sid"]},
            "client-fingerprint": "chrome",
        })
        # v3.4: Extra Reality proxies on alternate ports
        for extra_port in config.EXTRA_REALITY_PORTS:
            names.append(f"{label}-Port{extra_port}")
            proxies.append({
                "name": f"{label}-Port{extra_port}",
                "type": "vless",
                "server": config.TCP_PROXY_DOMAIN,
                "port": extra_port,
                "uuid": uid,
                "udp": True,
                "tls": True,
                "flow": "xtls-rprx-vision",
                "servername": _reality_sni(user.get("reality_sni", "")),
                "network": "tcp",
                "reality-opts": {"public-key": r["pub"], "short-id": r["sid"]},
                "client-fingerprint": "chrome",
            })

    doc = {
        "proxies": proxies,
        "proxy-groups": [
            {"name": "AURORA", "type": "select", "proxies": names or ["DIRECT"]}
        ],
        "rules": ["MATCH,AURORA"],
    }
    return _to_yaml(doc)


def _to_yaml(obj, indent: int = 0) -> str:
    pad = "  " * indent
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out.append(_to_yaml(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(v, (dict, list)):
                        out.append(f"{prefix}{k}:")
                        out.append(_to_yaml(v, indent + 2))
                    else:
                        out.append(f"{prefix}{k}: {_scalar(v)}")
            else:
                out.append(f"{pad}- {_scalar(item)}")
    return "\n".join(x for x in out if x)


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return (
        json.dumps(s)
        if (":" in s or s == "" or s[0] in "{[#&*!|>%@`")
        else s
    )
