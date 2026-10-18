from __future__ import annotations

import ipaddress
import json
import logging
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "intranet_auto_redirect_enabled": False,
    "intranet_use_proxy_host": False,
    "intranet_redirect_host": "",
    "intranet_redirect_port": 7777,
    "intranet_redirect_protocol": "http",
}
HEALTH_HEADERS = {
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Private-Network": "true",
    "X-Content-Type-Options": "nosniff",
}


def ensure_config(config_dir: Path) -> None:
    path = config_dir / "config.json"
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(DEFAULT_CONFIG, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        pass


def redirect_origin(config_dir: Path, settings: dict[str, Any]) -> str | None:
    """每次请求读取文件；配置缺失或无效时保持当前地址。"""
    path = config_dir / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("intranet_auto_redirect_enabled") is not True:
            return None
        host = settings.get("site_proxy_host", "") if raw.get("intranet_use_proxy_host") is True else raw.get("intranet_redirect_host", "")
        address = ipaddress.ip_address(str(host).strip().strip("[]"))
        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        if not any(address in ipaddress.ip_network(network) for network in networks):
            return None
        protocol = raw.get("intranet_redirect_protocol", "http")
        port_value = raw.get("intranet_redirect_port", 7777)
        if isinstance(port_value, bool):
            return None
        port = int(str(port_value))
        if protocol not in {"http", "https"} or not 1 <= port <= 65535:
            return None
        host_text = f"[{address}]" if address.version == 6 else str(address)
        default_port = 443 if protocol == "https" else 80
        return f"{protocol}://{host_text}" + (f":{port}" if port != default_port else "")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).warning("内网跳转配置无效，本次继续使用当前地址")
        return None
