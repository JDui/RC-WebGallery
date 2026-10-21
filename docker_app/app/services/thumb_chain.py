"""缩略图统一为 thumb 一档，沿用 small_thumb_url 和 .thumbs/small 存储兼容字段。

列表与详情优先此档，再回退视频封面；旧 thumb 派生图不再参与展示。
仅查看器可通过 original_last=True 显式回退原图。
"""

from __future__ import annotations

from typing import Iterable, Mapping

CARD_LEVELS: tuple[str, ...] = ("small_thumb_url", "cover_url")
PROMOTED_LEVELS: tuple[str, ...] = CARD_LEVELS
SMALL_LEVELS: tuple[str, ...] = ("small_thumb_url",)
INDEX_PREVIEW_LEVELS: tuple[str, ...] = CARD_LEVELS
INDEX_THUMB_LEVELS: tuple[str, ...] = CARD_LEVELS


def pick_thumb_url(
    sources: Iterable[Mapping[str, object] | None],
    *,
    levels: tuple[str, ...] = CARD_LEVELS,
    original_last: bool = False,
) -> str:
    """按级别外层、来源内层的顺序返回第一个非空 URL；全空返回 ""。

    original_last=True 时，所有级别落空后才按来源顺序回退到 ``url`` 字段（原图）。
    """
    source_list = [source for source in sources if source]
    for level in levels:
        for source in source_list:
            value = source.get(level)
            if value:
                return str(value)
    if original_last:
        for source in source_list:
            value = source.get("url")
            if value:
                return str(value)
    return ""
