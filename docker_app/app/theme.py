"""UI 配色派生：三个参数（色相 / 饱和度 / 明度）驱动整套界面颜色。

``app/static`` 里的样式表保留原始蓝紫色板作为基准（base palette）。前端请求
``/theme/<name>`` 时，本模块按 settings 表中保存的参数，把处于主题色相窗口内的
每一个颜色字面量重新映射，因此不需要改动 CSS 文件即可换肤。

三条规则：

* 色相以窗口内为基准整体重定位，窗口外的语义色（危险红、暖橙、状态绿、收藏粉）
  原样保留；
* 饱和度乘以 ``saturation``，``0.5`` 即"饱和度砍半"；
* 缩放饱和度会让颜色整体变亮，所以重新求解明度把 WCAG 相对亮度拉回原值，
  白色文字与按钮的可读性不随饱和度变化；``value`` 再在此基础上整体微调。
"""

from __future__ import annotations

import colorsys
import re
import threading
from pathlib import Path
from typing import Any

# 只有落在这个色相窗口（青 -> 蓝 -> 紫）里的颜色才属于"主题色"。
THEME_HUE_MIN = 170.0
THEME_HUE_MAX = 292.0
# 基准色板的主色色相（app.css ``--accent: #2d6cdf``），用作窗口重定位的锚点。
BASE_ANCHOR_HUE = 219.0
# 色相重定位时的离散系数：保留青/蓝/紫之间的相对差异，只压缩不抹平。
HUE_RATIO = 0.4

# rgb 三通道最大差值与 HSV 饱和度低于这两个阈值时视为中性灰，保持不变。
MIN_CHANNEL_DELTA = 2
MIN_SATURATION = 0.006
# 含有关键词的整行跳过（内网预检页的彩虹描边是刻意保留的装饰）。
SKIP_LINE_MARKER = ".intranet-edge"

LUMA_ITERATIONS = 22
CACHE_LIMIT = 32

DEFAULT_THEME: dict[str, float] = {"hue": 337.0, "saturation": 0.5, "value": 1.0}
THEME_BOUNDS: dict[str, tuple[float, float]] = {
    "hue": (0.0, 359.9),
    "saturation": (0.0, 1.5),
    "value": (0.55, 1.45),
}

_HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGB_RE = re.compile(
    r"(?P<fn>rgba?)\(\s*(?P<r>\d{1,3})\s*,\s*(?P<g>\d{1,3})\s*,\s*(?P<b>\d{1,3})\s*(?P<a>,\s*[\d.]+\s*)?\)"
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize(raw: Any) -> dict[str, float]:
    """把任意输入收敛成合法的主题参数（缺省用默认值，越界夹到边界）。"""
    source = raw if isinstance(raw, dict) else {}
    theme: dict[str, float] = {}
    for key in ("hue", "saturation", "value"):
        if key not in source or source[key] is None:
            theme[key] = DEFAULT_THEME[key]
            continue
        try:
            value = float(source[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要是数字") from exc
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{key} 需要是有限数字")
        theme[key] = round(clamp(value, *THEME_BOUNDS[key]), 3)
    return theme


def _channel_to_linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luma(rgb: tuple[int, int, int]) -> float:
    red, green, blue = rgb
    return (
        0.2126 * _channel_to_linear(red)
        + 0.7152 * _channel_to_linear(green)
        + 0.0722 * _channel_to_linear(blue)
    )


def _hsv_to_rgb(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    return tuple(round(channel * 255.0) for channel in colorsys.hsv_to_rgb(hue / 360.0, saturation, value))  # type: ignore[return-value]


def _value_for_luma(hue: float, saturation: float, target: float) -> float:
    """二分求值：在色相/饱和度固定时，找到相对亮度等于 target 的明度。"""
    if relative_luma(_hsv_to_rgb(hue, saturation, 1.0)) <= target:
        return 1.0
    if relative_luma(_hsv_to_rgb(hue, saturation, 0.0)) >= target:
        return 0.0
    low, high = 0.0, 1.0
    for _ in range(LUMA_ITERATIONS):
        middle = (low + high) / 2.0
        if relative_luma(_hsv_to_rgb(hue, saturation, middle)) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def transform_rgb(
    rgb: tuple[int, int, int], theme: dict[str, float]
) -> tuple[int, int, int]:
    """按主题参数重映射单个颜色；不属于主题色时原样返回。"""
    red, green, blue = rgb
    if max(rgb) - min(rgb) < MIN_CHANNEL_DELTA:
        return rgb
    hue, saturation, value = colorsys.rgb_to_hsv(red / 255.0, green / 255.0, blue / 255.0)
    hue *= 360.0
    if saturation < MIN_SATURATION or hue < THEME_HUE_MIN or hue > THEME_HUE_MAX:
        return rgb
    target_hue = (theme["hue"] + (hue - BASE_ANCHOR_HUE) * HUE_RATIO) % 360.0
    target_saturation = max(0.0, min(1.0, saturation * theme["saturation"]))
    target_value = clamp(_value_for_luma(target_hue, target_saturation, relative_luma(rgb)) * theme["value"], 0.0, 1.0)
    return _hsv_to_rgb(target_hue, target_saturation, target_value)


def _format_hex(digits: str, rgb: tuple[int, int, int]) -> str:
    rendered = "{:02x}{:02x}{:02x}".format(*rgb)
    if len(digits) == 3 and rendered[0] == rendered[1] and rendered[2] == rendered[3] and rendered[4] == rendered[5]:
        rendered = rendered[0] + rendered[2] + rendered[4]
    return ("#" + rendered).upper() if digits.isupper() else "#" + rendered


def _replace_hex(match: re.Match[str], theme: dict[str, float]) -> str:
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    rgb = (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
    return _format_hex(match.group(1), transform_rgb(rgb, theme))


def _replace_rgb(match: re.Match[str], theme: dict[str, float]) -> str:
    rgb = (int(match.group("r")), int(match.group("g")), int(match.group("b")))
    rendered = ", ".join(str(channel) for channel in transform_rgb(rgb, theme))
    alpha = match.group("a") or ""
    return f"{match.group('fn')}({rendered}{alpha})"


def transform_css(text: str, theme: dict[str, float]) -> str:
    """整份样式表按主题参数重写（逐行处理，跳过刻意保留装饰色的行）。

    基准色板是蓝紫，所以即便参数就是默认值也必须派生，不能短路返回原文。
    """
    lines = []
    for line in text.split("\n"):
        if SKIP_LINE_MARKER in line:
            lines.append(line)
            continue
        line = _RGB_RE.sub(lambda match: _replace_rgb(match, theme), line)
        line = _HEX_RE.sub(lambda match: _replace_hex(match, theme), line)
        lines.append(line)
    return "\n".join(lines)


def revision(theme: dict[str, float]) -> str:
    """给模板做 URL 缓存戳用：参数变了 URL 就变，浏览器立刻拿到新样式。"""
    return "h{hue:g}s{saturation:g}v{value:g}".format(**theme)


# 浏览器地址栏取色，基准与 liquid-glass.css 的 ``--bg-base`` 一致。
PAGE_TINT_BASE = (0xEA, 0xF0, 0xF8)


def page_tint(theme: dict[str, float]) -> str:
    """页面主底色（``<meta name="theme-color">`` 用），随配色参数一起派生。"""
    return "#{:02x}{:02x}{:02x}".format(*transform_rgb(PAGE_TINT_BASE, theme))


def swatches(theme: dict[str, float], base: dict[str, tuple[int, int, int]]) -> dict[str, str]:
    """把一组基准色派生成预览色块（``#rrggbb``），供设置页实时预览。"""
    return {key: "#{:02x}{:02x}{:02x}".format(*transform_rgb(value, theme)) for key, value in base.items()}


class CssThemeRenderer:
    """按 (文件, 参数) 缓存派生后的样式表，避免每次请求都重跑正则。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, int, int, str], bytes] = {}

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def render(self, path: Path, theme: dict[str, float]) -> tuple[bytes, str]:
        stat = path.stat()
        key = (path.name, stat.st_mtime_ns, stat.st_size, revision(theme))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached, key[3]
        text = path.read_text(encoding="utf-8")
        payload = transform_css(text, theme).encode("utf-8")
        with self._lock:
            if len(self._cache) >= CACHE_LIMIT:
                self._cache.clear()
            self._cache[key] = payload
        return payload, key[3]


css_renderer = CssThemeRenderer()
