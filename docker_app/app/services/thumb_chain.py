"""缩略图展示链的唯一出处。

全项目“同一个 pair 该用哪一档派生图展示”的选择逻辑都收敛到这里；
前端 app/static/app.js 顶部的 THUMB_LEVELS / THUMB_PROMOTED_LEVELS 是同名列镜像，
调整级别顺序时必须两端同步（tests/test_thumb_chain.py 有对拍用例）。

约定：
- 级别外层、来源内层（image → livephoto）：列表/详情永远优先选全局最小可用档位。
  例：image 缺 small 而 livephoto 有 small 时，选 livephoto.small 而不是 image.thumb。
- 原图（url）不进入列表/详情链，只有 viewer 底图经 original_last=True 显式放行兜底。
- duplicates.py 中感知哈希的文件优先级（small→thumb→原图，取“文件路径”而非 URL）
  是独立用途的策略，刻意不并入本模块。
"""

from __future__ import annotations

from typing import Iterable, Mapping

# 卡片/详情默认：small 优先，逐级升档，cover 兜 livephoto 封面。
CARD_LEVELS: tuple[str, ...] = ("small_thumb_url", "thumb_url", "cover_url")
# 详情滚动停止后的升档档与 pair_index 预览：thumb 优先。
PROMOTED_LEVELS: tuple[str, ...] = ("thumb_url", "small_thumb_url", "cover_url")
# 仅取 small 一档（folder_index 的 small_thumb_url 列，前端另有升档兜底）。
SMALL_LEVELS: tuple[str, ...] = ("small_thumb_url",)
# pair_index.preview_url：viewer 底图语义（thumb 优先 + 原图最后兜底）。
INDEX_PREVIEW_LEVELS: tuple[str, ...] = PROMOTED_LEVELS
# pair_index.thumb_url 列：thumb→cover→原图（与历史行为一致）。
INDEX_THUMB_LEVELS: tuple[str, ...] = ("thumb_url", "cover_url")


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
