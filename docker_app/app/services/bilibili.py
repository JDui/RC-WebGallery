from __future__ import annotations

import base64
import html
import io
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import qrcode
import requests

from app.db import Database
from app.services.legacy_bridge import API_DETAIL, API_FEED, build_headers, detail_params, feed_params
from app.services.utils import dumps_json, human_interval, loads_json, now_iso

API_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
API_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_SPACE_INFO = "https://api.bilibili.com/x/space/acc/info"
SPACE_HOME = "https://space.bilibili.com/{uid}"
AVATAR_FEED_FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,"
    "decorationCard,commentsNewVersion,onlyfansAssetsV2,ugcDelete,onlyfansQaCard"
)
# 允许被响应 Set-Cookie 回写覆盖的登录类 cookie。
# buvid3/buvid4/b_nut 是"设备指纹"，匿名预热请求（不带 Cookie 访问首页/空间页）
# 会返回全新的指纹；一旦合并进登录态，B 站按指纹漂移判定会话异常并吊销 SESSDATA。
COOKIE_REFRESH_ALLOWLIST = {
    "SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
    "sid", "bili_ticket", "bili_ticket_expires", "fetime", "fmtime", "login_time",
}
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    ),
]
ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.95,en;q=0.75",
    "zh-CN,zh;q=0.9,ja;q=0.7,en;q=0.6",
]


@dataclass
class CookieState:
    cookie: str | None
    cookie_json: dict[str, str]
    user: dict[str, Any]


class BilibiliAuthService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def start_qr_login(self) -> dict[str, Any]:
        session = self._qr_session()
        try:
            response = session.get(API_QR_GENERATE, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"二维码生成失败: {exc}") from exc
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "二维码生成失败")
        data = payload["data"]
        qr_url = data["url"]
        qr_key = data["qrcode_key"]
        self.db.update_auth_state(
            qr_key=qr_key,
            qr_url=qr_url,
            qr_status="pending",
            qr_created_at=now_iso(),
        )
        return {
            "qr_key": qr_key,
            "qr_url": qr_url,
            "image_data_url": self._render_qr_data_url(qr_url),
            "status": "pending",
        }

    def poll_qr_login(self) -> dict[str, Any]:
        auth_state = self.db.get_auth_state()
        qr_key = auth_state.get("qr_key")
        if not qr_key:
            return {"status": "idle", "message": "暂无二维码会话"}

        session = self._qr_session()
        try:
            response = session.get(API_QR_POLL, params={"qrcode_key": qr_key}, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"二维码轮询失败: {exc}") from exc
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "二维码轮询失败")

        data = payload.get("data", {})
        code = int(data.get("code", 0))
        message = data.get("message") or payload.get("message") or ""

        if code == 0:
            login_url = data.get("url", "")
            cookies = self._collect_login_cookies(response, login_url)
            self._save_cookie_state(cookies)
            self.db.update_auth_state(qr_status="done", qr_key=None, qr_url=None)
            return {"status": "done", "message": "登录成功", "user": self.check_cookie()}
        if code == 86038:
            self.db.update_auth_state(qr_status="expired")
            return {"status": "expired", "message": message or "二维码已过期"}
        if code == 86090:
            self.db.update_auth_state(qr_status="scanned")
            return {"status": "scanned", "message": message or "已扫码，等待确认"}
        if code == 86101:
            self.db.update_auth_state(qr_status="pending")
            return {"status": "pending", "message": message or "等待扫码"}

        return {"status": "pending", "message": message or "等待扫码"}

    def check_cookie(self) -> dict[str, Any]:
        cookie_state = self.get_cookie_state()
        if not cookie_state.cookie:
            return {"ok": False, "message": "未登录"}
        headers = self._headers(cookie_state.cookie)
        self._sleep_jitter(0.06, 0.12)
        try:
            response = requests.get(API_NAV, headers=headers, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            return {"ok": False, "message": f"Cookie 检查失败: {exc}"}
        if payload.get("code") != 0:
            return {"ok": False, "message": payload.get("message") or "Cookie 无效"}
        user_data = payload.get("data") or {}
        # 匿名访问 nav 同样返回 code 0，必须靠 isLogin 区分"登录态已被服务端吊销"。
        if not user_data.get("isLogin"):
            return {
                "ok": False,
                "message": "登录态已失效（B 站服务端未认可当前 Cookie），请重新扫码登录",
                "uid": str(user_data.get("mid") or ""),
                "uname": user_data.get("uname") or "",
                "user": None,
            }
        self.db.update_auth_state(user_json=dumps_json(user_data))
        return {
            "ok": True,
            "message": "Cookie 有效",
            "uid": str(user_data.get("mid") or ""),
            "uname": user_data.get("uname") or "",
            "user": {
                "uname": user_data.get("uname"),
                "mid": user_data.get("mid"),
                "face": user_data.get("face"),
                "level": user_data.get("level_info", {}).get("current_level"),
            },
        }

    def logout(self) -> None:
        self.db.update_auth_state(
            cookie=None,
            cookie_json=None,
            user_json=None,
            qr_key=None,
            qr_url=None,
            qr_status="idle",
            qr_created_at=None,
        )

    def get_cookie_state(self) -> CookieState:
        auth_state = self.db.get_auth_state()
        return CookieState(
            cookie=auth_state.get("cookie"),
            cookie_json=loads_json(auth_state.get("cookie_json"), {}),
            user=loads_json(auth_state.get("user_json"), {}),
        )

    def fetch_dynamic_detail(self, dynamic_id: str, host_mid: int, cookie: str) -> dict:
        last_error: Exception | None = None
        session = requests.Session()
        session.headers.update(self._headers(cookie, host_mid))
        session.cookies.update(self._parse_cookie_header(cookie))
        try:
            for attempt in range(6):
                if attempt:
                    self._refresh_web_session(session, host_mid, cookie)
                try:
                    self._sleep_jitter(0.08 + attempt * 0.05, 0.18 + attempt * 0.08)
                    response = session.get(API_DETAIL, headers=session.headers, params=detail_params(dynamic_id), timeout=30)
                    response.raise_for_status()
                    payload = response.json()
                    if int(payload.get("code", 0)) == -352 and attempt < 5:
                        last_error = RuntimeError("动态详情请求被风控拦截")
                        self._refresh_web_session(session, host_mid, cookie)
                        continue
                    if payload.get("code") != 0:
                        raise RuntimeError(payload.get("message") or "动态详情获取失败")
                    self._maybe_refresh_cookie_state(cookie, session.cookies)
                    data = payload.get("data") or {}
                    return data.get("item") or {}
                except requests.HTTPError as exc:
                    last_error = exc
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 412 and attempt < 5:
                        self._refresh_web_session(session, host_mid, cookie)
                        continue
                    break
                except requests.RequestException as exc:
                    last_error = exc
                    if attempt < 5:
                        self._refresh_web_session(session, host_mid, cookie)
                        continue
                    break
        finally:
            session.close()
        raise RuntimeError(f"动态详情请求失败: {last_error}")

    def fetch_up_profile(self, uid: str, cookie: str | None = None) -> dict[str, Any]:
        host_mid = int(uid)
        cookie_value = cookie or ""
        session = requests.Session()
        session.headers.update(self._headers(cookie_value, host_mid=host_mid))
        if cookie_value:
            session.cookies.update(self._parse_cookie_header(cookie_value))
        else:
            session.headers.pop("Cookie", None)
        last_error: Exception | None = None
        try:
            self._prime_profile_session(session, uid)
            for attempt in range(4):
                try:
                    if attempt:
                        self._refresh_profile_session(session, uid, cookie_value)
                    self._sleep_jitter(0.35 + attempt * 0.25, 0.75 + attempt * 0.35)
                    response = session.get(API_SPACE_INFO, params={"mid": str(uid)}, timeout=20)
                    response.raise_for_status()
                    payload = response.json()
                    code = int(payload.get("code", 0))
                    if code == 0:
                        data = payload.get("data") or {}
                        self._maybe_refresh_cookie_state(cookie_value, session.cookies)
                        face = data.get("face")
                        if self._should_lookup_dynamic_avatar(data):
                            dynamic_profile = self._fetch_up_profile_from_dynamic_feed(uid, session)
                            face = dynamic_profile.get("face") if dynamic_profile and dynamic_profile.get("face") else face
                        profile = {
                            "uid": str(uid),
                            "uname": data.get("name") or f"UID {uid}",
                            "face": face,
                        }
                        if profile["face"]:
                            return profile
                        fallback = self._fetch_up_profile_from_dynamic_feed(uid, session)
                        if not fallback or not fallback.get("face"):
                            fallback = self._fetch_up_profile_from_space_page(uid, session) or fallback
                        if fallback:
                            return {
                                "uid": str(uid),
                                "uname": profile["uname"] or fallback.get("uname") or f"UID {uid}",
                                "face": fallback.get("face") or profile["face"],
                            }
                        return profile
                    last_error = RuntimeError(payload.get("message") or "UP 主信息获取失败")
                    if code in (-352, -401) and attempt < 3:
                        continue
                    break
                except requests.HTTPError as exc:
                    last_error = exc
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 412 and attempt < 3:
                        continue
                    break
                except requests.RequestException as exc:
                    last_error = exc
                    if attempt < 3:
                        continue
                    break

            fallback = self._fetch_up_profile_from_dynamic_feed(uid, session)
            if not fallback or not fallback.get("face"):
                fallback = self._fetch_up_profile_from_space_page(uid, session) or fallback
            if fallback:
                return fallback
        finally:
            session.close()
        raise RuntimeError(f"UP 主信息获取失败: {last_error or '空间页解析失败'}")

    def _exchange_login(self, login_url: str) -> dict[str, str]:
        session = self._qr_session()
        session.get(login_url, allow_redirects=True, timeout=20)
        return self._extract_cookie_values(session.cookies)

    def _collect_login_cookies(self, response: requests.Response, login_url: str) -> dict[str, str]:
        cookies = self._extract_cookie_values(response.cookies)
        if login_url:
            cookies.update(self._extract_cookie_values_from_url(login_url))
        if not cookies.get("SESSDATA") and login_url:
            cookies.update(self._exchange_login(login_url))
        if not cookies.get("SESSDATA"):
            raise RuntimeError("扫码完成，但未拿到登录 Cookie")
        return cookies

    def _save_cookie_state(self, cookies: dict[str, str]) -> None:
        cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
        self.db.update_auth_state(cookie=cookie, cookie_json=dumps_json(cookies))
        self.check_cookie()

    def _headers(self, cookie: str, host_mid: int = 31968078) -> dict[str, str]:
        headers = dict(build_headers(host_mid=host_mid, cookie=cookie))
        headers["User-Agent"] = random.choice(USER_AGENTS)
        headers["Accept-Language"] = random.choice(ACCEPT_LANGUAGES)
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
        headers["DNT"] = "1"
        headers["Priority"] = random.choice(["u=1, i", "u=2, i", "u=3"])
        headers["Sec-CH-UA"] = '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="24"'
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = random.choice(['"Windows"', '"macOS"', '"Linux"'])
        return headers

    def _qr_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
                "Referer": "https://passport.bilibili.com/login",
                "Origin": "https://passport.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        return session

    def _render_qr_data_url(self, qr_url: str) -> str:
        image = qrcode.make(qr_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _extract_cookie_values(self, cookie_source: Any) -> dict[str, str]:
        if hasattr(cookie_source, "items"):
            return {str(name): str(value) for name, value in cookie_source.items() if value}
        return {}

    def _extract_cookie_values_from_url(self, login_url: str) -> dict[str, str]:
        query = parse_qs(urlparse(login_url).query)
        cookie_names = ["SESSDATA", "bili_jct", "DedeUserID"]
        return {
            name: values[0]
            for name in cookie_names
            if (values := query.get(name))
        }

    def _parse_cookie_header(self, cookie: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for chunk in cookie.split(";"):
            if "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                result[name] = value
        return result

    def _prime_profile_session(self, session: requests.Session, uid: str) -> None:
        urls = [
            "https://www.bilibili.com/",
            SPACE_HOME.format(uid=uid),
            f"{SPACE_HOME.format(uid=uid)}/dynamic",
        ]
        for url in urls:
            try:
                session.get(url, timeout=15)
                self._sleep_jitter(0.25, 0.55)
            except requests.RequestException:
                continue

    def _refresh_profile_session(self, session: requests.Session, uid: str, cookie: str) -> None:
        session.cookies.clear()
        if cookie:
            session.cookies.update(self._parse_cookie_header(cookie))
        session.headers.clear()
        session.headers.update(self._headers(cookie, host_mid=int(uid)))
        if not cookie:
            session.headers.pop("Cookie", None)
        self._prime_profile_session(session, uid)

    def _fetch_up_profile_from_space_page(
        self,
        uid: str,
        session: requests.Session,
    ) -> dict[str, Any] | None:
        try:
            self._sleep_jitter(0.7, 1.2)
            response = session.get(SPACE_HOME.format(uid=uid), timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            return None
        uname = self._extract_space_uname(response.text)
        face = self._extract_space_face(response.text)
        if not uname and not face:
            return None
        return {
            "uid": str(uid),
            "uname": uname or f"UID {uid}",
            "face": face,
        }

    def _fetch_up_profile_from_dynamic_feed(
        self,
        uid: str,
        session: requests.Session,
    ) -> dict[str, Any] | None:
        original_headers = dict(session.headers)
        header_variants = [original_headers, self._dynamic_feed_headers(uid, original_headers.get("Cookie"))]
        try:
            for index, headers in enumerate(header_variants):
                try:
                    session.headers.clear()
                    session.headers.update(headers)
                    if index:
                        self._sleep_jitter(0.45, 0.85)
                    else:
                        self._sleep_jitter(0.35, 0.75)
                    response = session.get(API_FEED, params=self._avatar_feed_params(uid), timeout=20)
                    response.raise_for_status()
                    payload = response.json()
                except (ValueError, requests.RequestException):
                    continue
                if int(payload.get("code", 0)) != 0:
                    continue
                profile = self._extract_dynamic_feed_profile(payload.get("data") or {}, uid)
                if profile:
                    return profile
        finally:
            session.headers.clear()
            session.headers.update(original_headers)
        return None

    def _dynamic_feed_headers(self, uid: str, cookie: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": random.choice(ACCEPT_LANGUAGES),
            "Referer": f"https://space.bilibili.com/{uid}/dynamic",
            "Connection": "close",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _avatar_feed_params(self, uid: str) -> dict[str, str]:
        params = feed_params(int(uid), "")
        params["features"] = AVATAR_FEED_FEATURES
        return params

    def _extract_dynamic_feed_profile(self, data: dict[str, Any], uid: str) -> dict[str, Any] | None:
        uid_text = str(uid)
        authors: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            author = ((item.get("modules") or {}).get("module_author") or {})
            if isinstance(author, dict):
                authors.append(author)
        authors.sort(key=lambda author: 0 if str(author.get("mid") or "") == uid_text else 1)
        for author in authors:
            face = self._extract_author_avatar_url(author)
            uname = str(author.get("name") or "").strip()
            if face or uname:
                return {
                    "uid": uid_text,
                    "uname": uname or f"UID {uid}",
                    "face": face,
                }
        return None

    def _extract_author_avatar_url(self, author: dict[str, Any]) -> str | None:
        avatar = author.get("avatar")
        if isinstance(avatar, dict):
            if url := self._extract_avatar_layers_url(avatar.get("fallback_layers"), prefer_animation=False):
                return url
        if url := self._clean_profile_image_url(author.get("face"), allow_dynamic=True):
            return url
        if isinstance(avatar, dict):
            if url := self._extract_avatar_layers_url(avatar.get("layers"), prefer_animation=True):
                return url
        return None

    def _extract_avatar_layers_url(self, value: Any, prefer_animation: bool) -> str | None:
        groups = value if isinstance(value, list) else [value]
        resource_order = ("res_animation", "res_image") if prefer_animation else ("res_image", "res_animation")
        for group in groups:
            layers = group.get("layers") if isinstance(group, dict) else None
            for layer in (layers if isinstance(layers, list) else []):
                if not isinstance(layer, dict):
                    continue
                for resource_key in resource_order:
                    resource = (layer.get("resource") or {}).get(resource_key)
                    if url := self._find_avatar_resource_url(resource):
                        return url
        return None

    def _find_avatar_resource_url(self, value: Any) -> str | None:
        if isinstance(value, dict):
            if url := self._clean_profile_image_url(value.get("url"), allow_dynamic=True):
                return url
            for child in value.values():
                if url := self._find_avatar_resource_url(child):
                    return url
        if isinstance(value, list):
            for item in value:
                if url := self._find_avatar_resource_url(item):
                    return url
        return None

    def _should_lookup_dynamic_avatar(self, data: dict[str, Any]) -> bool:
        face = self._clean_profile_image_url(data.get("face"), allow_dynamic=True) or str(data.get("face") or "")
        parsed = urlparse(face)
        path = parsed.path.lower()
        return bool(data.get("face_nft") or data.get("face_nft_type") or "/bfs/baselabs/" in path)

    def _extract_space_uname(self, text: str) -> str | None:
        title_match = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        if not title_match:
            return None
        uname = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()
        if not uname:
            return None
        if "的个人空间" in uname:
            uname = uname.split("的个人空间", 1)[0].strip()
        else:
            uname = re.split(r"\s*[-_]\s*", uname, maxsplit=1)[0].strip()
        for marker in ("个人主页", "哔哩哔哩视频", "_哔哩哔哩"):
            uname = uname.replace(marker, "")
        uname = re.sub(r"\s+", " ", uname).strip()
        return uname or None

    def _extract_space_face(self, text: str) -> str | None:
        patterns = [
            r'"(?:face|avatar|avatar_url)"\s*:\s*"([^"]+)"',
            r"https?:\\?/\\?/[^\"'<>\s]+/bfs/(?:face|baselabs)/[^\"'<>\s]+",
            r"//[^\"'<>\s]+/bfs/(?:face|baselabs)/[^\"'<>\s]+",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                raw_url = match.group(1) if match.groups() else match.group(0)
                if url := self._clean_profile_image_url(raw_url, allow_dynamic=True):
                    return url
        for tag in re.findall(r"<img\b[^>]*>", text, flags=re.I):
            attrs = self._html_attrs(tag)
            marker = " ".join(str(attrs.get(key) or "") for key in ("class", "id", "alt")).lower()
            if any(keyword in marker for keyword in ("avatar", "face", "头像")):
                if url := self._clean_profile_image_url(attrs.get("src") or attrs.get("data-src"), allow_dynamic=True):
                    return url
        for tag in re.findall(r"<meta\b[^>]*>", text, flags=re.I):
            attrs = self._html_attrs(tag)
            key = str(attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            if key in {"og:image", "twitter:image", "twitter:image:src", "image"}:
                if url := self._clean_profile_image_url(attrs.get("content"), allow_dynamic=True):
                    return url
        if url := self._extract_layered_avatar_url(text):
            return url
        return None

    def _extract_layered_avatar_url(self, text: str) -> str | None:
        patterns = [
            r'"bfs_style"\s*:\s*"widget-layer-avatar".{0,700}?"url"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"([^"]+)".{0,700}?"bfs_style"\s*:\s*"widget-layer-avatar"',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I | re.S):
                if url := self._clean_profile_image_url(match.group(1), allow_dynamic=True):
                    return url
        return None

    def _clean_profile_image_url(self, value: Any, allow_dynamic: bool = False) -> str | None:
        raw = html.unescape(str(value or "")).strip()
        if not raw:
            return None
        raw = raw.replace("\\/", "/")
        if "\\u" in raw or "\\x" in raw:
            try:
                raw = json.loads(f'"{raw}"')
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        raw = raw.strip()
        if raw.startswith("//"):
            raw = f"https:{raw}"
        if raw.startswith("http://"):
            raw = f"https://{raw[7:]}"
        if not re.match(r"^https://", raw, flags=re.I):
            return None
        if not self._looks_like_profile_image_url(raw, allow_dynamic=allow_dynamic):
            return None
        return raw

    def _looks_like_profile_image_url(self, url: str, allow_dynamic: bool = False) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if not host.endswith(("hdslb.com", "bilivideo.com", "bilibili.com")):
            return False
        if path.endswith((".ico", ".svg")):
            return False
        if any(marker in path for marker in ("favicon", "logo", "webicon", "apple-touch-icon")):
            return False
        if "/bfs/face/" in path or "/images/member/noface" in path:
            return True
        return allow_dynamic and (
            "/bfs/garb/item/" in path
            or "/bfs/baselabs/" in path
            or "/bfs/activity-plat/static/" in path
        )

    def _html_attrs(self, tag: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for match in re.finditer(r"([:\w-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag):
            key = match.group(1).lower()
            value = match.group(2).strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            attrs[key] = html.unescape(value)
        return attrs

    def _prime_web_session(self, session: requests.Session, host_mid: int) -> None:
        urls = [
            "https://www.bilibili.com/",
            f"https://space.bilibili.com/{host_mid}",
            f"https://space.bilibili.com/{host_mid}/dynamic",
            API_NAV,
        ]
        for url in urls:
            try:
                session.get(url, timeout=15)
                self._sleep_jitter(0.05, 0.1)
            except requests.RequestException:
                continue

    def _refresh_web_session(self, session: requests.Session, host_mid: int, cookie: str) -> None:
        session.cookies.clear()
        session.cookies.update(self._parse_cookie_header(cookie))
        session.headers.clear()
        session.headers.update(self._headers(cookie, host_mid))
        self._prime_web_session(session, host_mid)

    def _maybe_refresh_cookie_state(self, cookie: str, cookie_source: Any) -> None:
        if not cookie:
            return
        current = self._parse_cookie_header(cookie)
        issued = self._extract_cookie_values(cookie_source)
        updates = {
            name: value
            for name, value in issued.items()
            if name in COOKIE_REFRESH_ALLOWLIST and value and current.get(name) != value
        }
        if not updates:
            return
        merged = {**current, **updates}
        cookie_value = "; ".join(f"{name}={value}" for name, value in merged.items())
        self.db.update_auth_state(cookie=cookie_value, cookie_json=dumps_json(merged))

    def _sleep_jitter(self, base: float, spread: float) -> None:
        delay = random.uniform(max(0.04, base * 0.7), base + spread)
        # 拟人化下限：对 B 站的每次操作间隔不低于 250~880ms 随机时长。
        time.sleep(max(delay, human_interval()))
