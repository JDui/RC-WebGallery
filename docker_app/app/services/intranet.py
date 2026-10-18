from __future__ import annotations

import ipaddress
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "intranet_auto_redirect_enabled": False,
    "intranet_use_proxy_host": False,
    "intranet_redirect_host": "",
    "intranet_redirect_port": 7777,
    "intranet_redirect_protocol": "http",
}
_config_lock = threading.RLock()

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


def is_local_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(str(host).strip().strip("[]"))
        return any(address in ipaddress.ip_network(network) for network in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
    except ValueError:
        return False


def _read_document(config_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取内网配置，请检查 config.json 的权限和 JSON 格式") from exc
    if not isinstance(raw, dict):
        raise ValueError("内网配置 config.json 必须是 JSON 对象")
    return raw


def read_config(config_dir: Path) -> dict[str, Any]:
    raw = _read_document(config_dir)
    return {key: raw.get(key, default) for key, default in DEFAULT_CONFIG.items()}


def _protocol_and_port(values: dict[str, Any]) -> tuple[str, int]:
    protocol = values.get("intranet_redirect_protocol", "http")
    port_value = values.get("intranet_redirect_port", 7777)
    if isinstance(port_value, bool):
        raise ValueError("内网端口必须是 1 至 65535 的整数")
    try:
        port = int(str(port_value))
    except (ValueError, TypeError) as exc:
        raise ValueError("内网端口必须是 1 至 65535 的整数") from exc
    if protocol not in ("http", "https") or not 1 <= port <= 65535:
        raise ValueError("内网协议必须是 http 或 https，端口必须在 1 至 65535 之间")
    return protocol, port


def configured_origin(values: dict[str, Any], settings: dict[str, Any]) -> str:
    host = settings.get("site_proxy_host", "") if values.get("intranet_use_proxy_host") is True else values.get("intranet_redirect_host", "")
    if not is_local_host(host):
        raise ValueError("内网 IP 必须是 RFC1918 IPv4 或 IPv6 ULA 地址；共用代理 IP 时也不能使用 127.0.0.1")
    address = ipaddress.ip_address(str(host).strip().strip("[]"))
    protocol, port = _protocol_and_port(values)
    host_text = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if protocol == "https" else 80
    return f"{protocol}://{host_text}" + (f":{port}" if port != default_port else "")


def save_config(config_dir: Path, updates: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """与设置页共用同一份文件，原子替换并保留其他配置项。"""
    with _config_lock:
        raw = _read_document(config_dir)
        values = {**DEFAULT_CONFIG, **{key: value for key, value in raw.items() if key in DEFAULT_CONFIG},
                  **{key: value for key, value in updates.items() if key in DEFAULT_CONFIG}}
        for key in ("intranet_auto_redirect_enabled", "intranet_use_proxy_host"):
            if not isinstance(values[key], bool):
                raise ValueError("内网开关必须是布尔值")
        if not isinstance(values["intranet_redirect_host"], str):
            raise ValueError("内网 IP 必须是文本地址")
        values["intranet_redirect_host"] = values["intranet_redirect_host"].strip()
        protocol, port = _protocol_and_port(values)
        values["intranet_redirect_protocol"] = protocol
        values["intranet_redirect_port"] = port
        if values["intranet_auto_redirect_enabled"]:
            configured_origin(values, settings)
        path = config_dir / "config.json"
        temporary = None
        try:
            raw.update(values)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config_dir, prefix=".intranet-", delete=False) as handle:
                temporary = Path(handle.name)
                if path.exists():
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                json.dump(raw, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("内网配置保存失败，请检查 config.json 的格式和目录写入权限") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return values


def redirect_origin(config_dir: Path, settings: dict[str, Any]) -> str | None:
    """每次请求读取文件；配置缺失或无效时保持当前地址。"""
    try:
        values = read_config(config_dir)
        if values["intranet_auto_redirect_enabled"] is not True:
            return None
        return configured_origin(values, settings)
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("内网跳转配置无效，本次继续使用当前地址")
        return None
