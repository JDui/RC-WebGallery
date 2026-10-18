from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services.intranet import DEFAULT_CONFIG, ensure_config, redirect_origin


def write_config(tmp_path, **changes):
    payload = {**DEFAULT_CONFIG, "intranet_redirect_host": "192.168.31.221", **changes}
    (tmp_path / "config.json").write_text(json.dumps(payload))


def test_default_is_off_and_existing_file_is_preserved(tmp_path):
    ensure_config(tmp_path)
    assert redirect_origin(tmp_path, {}) is None
    write_config(tmp_path, intranet_auto_redirect_enabled=True)
    ensure_config(tmp_path)
    assert redirect_origin(tmp_path, {}) == "http://192.168.31.221:7777"


def test_file_changes_take_effect_and_proxy_host_does_not_share_port(tmp_path):
    settings = {"site_proxy_host": "192.168.31.222", "site_proxy_port": 7890, "site_proxy_enabled": False}
    write_config(tmp_path, intranet_auto_redirect_enabled=True, intranet_use_proxy_host=True)
    assert redirect_origin(tmp_path, settings) == "http://192.168.31.222:7777"
    write_config(tmp_path, intranet_auto_redirect_enabled=False)
    assert redirect_origin(tmp_path, settings) is None


@pytest.mark.parametrize("changes", [
    {"intranet_auto_redirect_enabled": "false"},
    {"intranet_redirect_host": "example.org"},
    {"intranet_redirect_host": "127.0.0.1"},
    {"intranet_redirect_host": "8.8.8.8"},
    {"intranet_redirect_host": "192.168.1.1/evil"},
    {"intranet_redirect_port": 0},
    {"intranet_redirect_port": 65536},
    {"intranet_redirect_port": True},
    {"intranet_redirect_protocol": "javascript"},
])
def test_invalid_config_fails_closed(tmp_path, changes):
    write_config(tmp_path, **{"intranet_auto_redirect_enabled": True, **changes})
    assert redirect_origin(tmp_path, {}) is None


def test_malformed_config_and_ipv6(tmp_path):
    (tmp_path / "config.json").write_text("{")
    assert redirect_origin(tmp_path, {}) is None
    write_config(tmp_path, intranet_auto_redirect_enabled=True, intranet_redirect_host="fd12::1", intranet_redirect_protocol="https", intranet_redirect_port=443)
    assert redirect_origin(tmp_path, {}) == "https://[fd12::1]"


def test_http_preflight_bypass_identity_and_default_port(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "config", SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(main, "db", SimpleNamespace(get_settings=lambda: {}))
    write_config(tmp_path, intranet_auto_redirect_enabled=True)
    client = TestClient(main.app)
    response = client.get("/?q=hello")
    assert 'data-intranet-origin="http://192.168.31.221:7777"' in response.text
    assert "_gallery_skip_intranet=1" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/intranet/health").json() == {"ok": True, "service": "biligalleryrc"}
    probe = client.options("/intranet/health", headers={"Origin": "https://example.test", "Access-Control-Request-Private-Network": "true"})
    assert probe.headers["access-control-allow-private-network"] == "true"
    assert probe.content == b""
    assert 'data-intranet-origin' not in client.get("/?_gallery_skip_intranet=1").text
    assert 'data-intranet-origin' not in TestClient(main.app, base_url="http://192.168.31.221:7777").get("/").text
    write_config(tmp_path, intranet_auto_redirect_enabled=True, intranet_redirect_port=80)
    assert 'data-intranet-origin' not in TestClient(main.app, base_url="http://192.168.31.221:80").get("/").text
    write_config(tmp_path, intranet_auto_redirect_enabled=False)
    assert 'data-intranet-origin' not in client.get("/").text
