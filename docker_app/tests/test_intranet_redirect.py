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


def test_settings_api_and_file_share_one_source(tmp_path, monkeypatch):
    from app import main
    saved = {}
    stored = {"site_proxy_host": "192.168.31.222", "site_proxy_port": 7890}
    def persist(payload):
        saved.update(payload)
        stored.update(payload)
        return dict(stored)
    monkeypatch.setattr(main, "config", SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(main, "db", SimpleNamespace(get_settings=lambda: dict(stored), save_settings=persist))
    monkeypatch.setattr(main, "auth", SimpleNamespace(check_cookie=lambda: {"ok": False}))
    monkeypatch.setattr(main, "thumbnailer", SimpleNamespace(apply_settings=lambda _: None))
    monkeypatch.setattr(main, "scheduler", SimpleNamespace(reload=lambda: None))
    (tmp_path / "config.json").write_text(json.dumps({"other_setting": "preserved"}))
    client = TestClient(main.app)
    assert client.get("/api/settings").json()["intranet_auto_redirect_enabled"] is False
    response = client.put("/api/settings", json={
        **DEFAULT_CONFIG, "intranet_auto_redirect_enabled": True,
        "intranet_use_proxy_host": True, "app_title": "demo",
    })
    assert response.status_code == 200
    assert response.json()["intranet_auto_redirect_enabled"] is True
    assert saved == {"app_title": "demo"}
    assert json.loads((tmp_path / "config.json").read_text())["other_setting"] == "preserved"
    assert redirect_origin(tmp_path, stored) == "http://192.168.31.222:7777"
    independent = client.put("/api/settings", json={"intranet_use_proxy_host": False, "intranet_redirect_host": "192.168.31.221"})
    assert independent.status_code == 200
    assert independent.json()["site_proxy_host"] == "192.168.31.222"
    assert redirect_origin(tmp_path, stored) == "http://192.168.31.221:7777"
    assert client.put("/api/settings", json={"intranet_use_proxy_host": True}).status_code == 200
    assert redirect_origin(tmp_path, stored) == "http://192.168.31.222:7777"
    assert client.put("/api/settings", json={"intranet_use_proxy_host": False}).status_code == 200
    assert redirect_origin(tmp_path, stored) == "http://192.168.31.221:7777"
    write_config(tmp_path, intranet_auto_redirect_enabled=False)
    assert client.get("/api/settings").json()["intranet_auto_redirect_enabled"] is False
    assert 'data-intranet-origin' not in client.get("/").text
    response = client.put("/api/settings", json={"intranet_auto_redirect_enabled": True, "intranet_redirect_host": "8.8.8.8", "app_title": "bad"})
    assert response.status_code == 400
    assert stored["app_title"] == "demo"
    assert client.get("/api/settings").json()["intranet_auto_redirect_enabled"] is False
    (tmp_path / "config.json").write_text("{")
    broken = client.get("/api/settings")
    assert broken.status_code == 200
    assert "JSON" in broken.json()["intranet_config_error"]
    assert broken.json()["intranet_auto_redirect_enabled"] is False
    assert 'data-intranet-access=' in client.get("/").text
    assert client.put("/api/settings", json={"intranet_auto_redirect_enabled": False}).status_code == 400
    assert (tmp_path / "config.json").read_text() == "{"


def test_atomic_save_keeps_existing_config_on_failure(tmp_path, monkeypatch):
    from app.services import intranet
    write_config(tmp_path)
    before = (tmp_path / "config.json").read_text()
    def fail_replace(*_):
        raise OSError("read only")
    monkeypatch.setattr(intranet.os, "replace", fail_replace)
    with pytest.raises(ValueError, match="保存失败"):
        intranet.save_config(tmp_path, {"intranet_auto_redirect_enabled": True}, {})
    assert (tmp_path / "config.json").read_text() == before
    assert not list(tmp_path.glob(".intranet-*"))


@pytest.mark.parametrize("host, expected", [
    ("192.168.31.221", "local"), ("10.1.2.3", "local"),
    ("[fd12::1]", "local"), ("127.0.0.1", "external"),
    ("8.8.8.8", "external"), ("gallery.example", "external"),
])
def test_rainbow_marks_actual_private_address_even_when_redirect_disabled(tmp_path, monkeypatch, host, expected):
    from app import main
    monkeypatch.setattr(main, "config", SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(main, "db", SimpleNamespace(get_settings=lambda: {}))
    write_config(tmp_path, intranet_auto_redirect_enabled=False)
    response = TestClient(main.app).get("/", headers={"host": f"{host}:7777"})
    assert f'data-intranet-access="{expected}"' in response.text
    assert 'class="intranet-edge" aria-hidden="true"' in response.text


@pytest.mark.parametrize("updates", [
    {"intranet_redirect_port": True}, {"intranet_redirect_port": 65536},
    {"intranet_redirect_port": 12.5}, {"intranet_redirect_protocol": {}},
    {"intranet_auto_redirect_enabled": "false"}, {"intranet_redirect_host": []},
])
def test_invalid_settings_do_not_overwrite_file_even_when_disabled(tmp_path, updates):
    from app.services.intranet import save_config
    write_config(tmp_path)
    before = (tmp_path / "config.json").read_text()
    with pytest.raises(ValueError):
        save_config(tmp_path, updates, {})
    assert (tmp_path / "config.json").read_text() == before
