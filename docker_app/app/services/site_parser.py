from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

from app.services.utils import parse_date, safe_slug

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - fallback is covered through parser behavior
    BeautifulSoup = None


@dataclass
class ParsedAsset:
    url: str
    media_type: str


@dataclass
class ParsedPost:
    url: str
    title: str
    pub_date: str | None
    tags: list[str] = field(default_factory=list)
    excerpt: str = ""
    assets: list[ParsedAsset] = field(default_factory=list)


DEFAULT_SITE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_DATE_SELECTOR = (
    "time, .entry-date, .date, .updated, .published, .posted-on, .post-date, "
    ".entry-meta, .post-meta, .meta-date, .image-info-time, .item_images_info, "
    ".library-date, .library-time, .card-date, .item-date, [datetime]"
)
DEFAULT_TAG_SELECTOR = ".tag, .tags a, .cat-name, .category a, .post-categories a"
DEFAULT_BODY_SELECTOR = "article, .entry-content, .post-content, .post-page-content, .library-content, .gallery-content, .content, main"
DEFAULT_MEDIA_SELECTOR = (
    "article img, article video, article source, .entry-content img, .post-content img, "
    ".post-page-content img, .library-content img, .gallery-content img, .content img, "
    ".content video, .content source, main img"
)


def browser_like_site_headers(user_agent: str | None = None) -> dict[str, str]:
    return {
        "User-Agent": user_agent or DEFAULT_SITE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7,zh-CN;q=0.6",
        "Connection": "close",
        "Upgrade-Insecure-Requests": "1",
    }


def site_request_timeout(read_timeout: int | float) -> tuple[float, float]:
    read_seconds = max(float(read_timeout or 300), 30.0)
    connect_seconds = min(20.0, max(10.0, read_seconds / 6))
    return (connect_seconds, read_seconds)


def decode_site_response(response: requests.Response) -> str:
    content = response.content
    if not content:
        return ""

    bom_encodings = (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )
    for bom, encoding in bom_encodings:
        if content.startswith(bom):
            return content.decode(encoding, errors="replace")

    head = content[:8192].decode("ascii", errors="ignore")
    meta_match = re.search(
        r"""<meta\b[^>]*\bcharset\s*=\s*["']?\s*([a-zA-Z0-9._-]+)""",
        head,
        re.IGNORECASE,
    )
    if not meta_match:
        meta_match = re.search(
            r"""<meta\b[^>]*\bcontent\s*=\s*["'][^"']*charset\s*=\s*([a-zA-Z0-9._-]+)""",
            head,
            re.IGNORECASE,
        )

    content_type = str(response.headers.get("content-type") or "")
    header_match = re.search(r"charset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)", content_type, re.IGNORECASE)
    candidates = [
        meta_match.group(1) if meta_match else None,
        header_match.group(1) if header_match else None,
        "utf-8",
        response.apparent_encoding,
        "gb18030",
    ]
    seen: set[str] = set()
    for encoding in candidates:
        normalized = str(encoding or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return content.decode(normalized)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


class PageFetcher:
    def __init__(
        self,
        timeout: int = 300,
        user_agent: str = DEFAULT_SITE_USER_AGENT,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.timeout = site_request_timeout(timeout)
        self.session = requests.Session()
        self.session.headers.update(browser_like_site_headers(user_agent))
        if proxies:
            self.session.proxies.update(proxies)

    def get_text(self, url: str) -> str:
        if url.startswith("file://"):
            return Path(urlparse(url).path).read_text(encoding="utf-8")
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return decode_site_response(response)


class HtmlNode:
    def __init__(self, name: str, attrs: dict[str, str] | None = None) -> None:
        self.name = name
        self.attrs = attrs or {}
        self.children: list[HtmlNode] = []
        self.text_parts: list[str] = []

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.attrs.get(key, default)

    def get_text(self, separator: str = " ", strip: bool = True) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.get_text(separator=separator, strip=strip))
        text = separator.join(part for part in parts if part)
        return " ".join(text.split()) if strip else text

    def select_one(self, selector: str) -> "HtmlNode | None":
        items = self.select(selector)
        return items[0] if items else None

    def select(self, selector: str) -> list["HtmlNode"]:
        results: list[HtmlNode] = []
        seen: set[int] = set()
        for group in [item.strip() for item in selector.split(",") if item.strip()]:
            matched = self._select_group(group)
            for node in matched:
                if id(node) not in seen:
                    seen.add(id(node))
                    results.append(node)
        return results

    def _select_group(self, selector: str) -> list["HtmlNode"]:
        current = [self]
        for part in selector.split():
            next_nodes: list[HtmlNode] = []
            for node in current:
                next_nodes.extend([item for item in node._descendants() if _matches_selector(item, part)])
            current = next_nodes
        return current

    def _descendants(self) -> list["HtmlNode"]:
        output: list[HtmlNode] = []
        for child in self.children:
            output.append(child)
            output.extend(child._descendants())
        return output


class SimpleHtmlParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        while len(self.stack) > 1:
            node = self.stack.pop()
            if node.name == tag:
                break

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].text_parts.append(data.strip())


def parse_html(text: str) -> Any:
    if BeautifulSoup is not None:
        return BeautifulSoup(text, "lxml")
    parser = SimpleHtmlParser()
    parser.feed(text)
    return parser.root


def _matches_selector(node: HtmlNode, selector: str) -> bool:
    attr_name = attr_operator = attr_value = None
    if "[" in selector and selector.endswith("]"):
        selector, attr_part = selector.split("[", 1)
        attr_part = attr_part[:-1]
        for operator in ("*=", "$=", "^=", "="):
            if operator in attr_part:
                attr_name, attr_value = attr_part.split(operator, 1)
                attr_operator = operator
                attr_name = attr_name.strip().lower()
                attr_value = attr_value.strip().strip("'\"")
                break
        if not attr_name:
            attr_name = attr_part.strip().lower()
            attr_operator = "exists"
    tag = ""
    class_name = ""
    element_id = ""
    if "#" in selector:
        selector, element_id = selector.split("#", 1)
    if "." in selector:
        tag, class_name = selector.split(".", 1)
    elif selector:
        tag = selector
    if tag and node.name != tag.lower():
        return False
    if class_name:
        classes = (node.get("class") or "").split()
        required_classes = [item for item in class_name.split(".") if item]
        if any(item not in classes for item in required_classes):
            return False
    if element_id and node.get("id") != element_id:
        return False
    if attr_name and attr_operator == "exists":
        return node.get(attr_name.lower()) is not None
    if attr_name and attr_operator:
        value = node.get(attr_name.lower()) or ""
        lower_value = value.lower()
        lower_attr_value = (attr_value or "").lower()
        if attr_operator == "*=" and lower_attr_value not in lower_value:
            return False
        if attr_operator == "$=" and not lower_value.endswith(lower_attr_value):
            return False
        if attr_operator == "^=" and not lower_value.startswith(lower_attr_value):
            return False
        if attr_operator == "=" and lower_value != lower_attr_value:
            return False
    return True


class SourceParser:
    def __init__(self, fetcher: PageFetcher) -> None:
        self.fetcher = fetcher

    def suggest(self, entry_url: str) -> dict[str, Any]:
        entry_url = str(entry_url or "").strip()
        if not entry_url:
            raise ValueError("请输入入口 URL")
        text = self.fetcher.get_text(entry_url)
        xml_type, xml_title = self._xml_source_hint(text)
        if xml_type:
            name = xml_title or self._url_site_name(entry_url)
            return {
                "name": name,
                "slug": safe_slug(name, "source"),
                "source_type": xml_type,
                "entry_url": entry_url,
                "page_url_template": "",
                "max_pages": 1,
                "list_item_selector": "",
                "detail_link_selector": "a",
                "title_selector": "h1",
                "date_selector": DEFAULT_DATE_SELECTOR,
                "tag_selector": DEFAULT_TAG_SELECTOR,
                "body_selector": DEFAULT_BODY_SELECTOR,
                "media_selector": DEFAULT_MEDIA_SELECTOR,
                "skip_head_images": 0,
                "skip_tail_images": 0,
                "enabled": True,
                "start_date": "",
                "confidence": 90,
                "message": "已识别为 RSS" if xml_type == "rss" else "已识别为 Sitemap",
                "preview": [],
            }

        soup = parse_html(text)
        name = self._html_site_name(soup, entry_url)
        html_hint = self._html_source_hint(soup, entry_url)
        return {
            "name": name,
            "slug": safe_slug(name, "source"),
            "source_type": "html",
            "entry_url": entry_url,
            "page_url_template": html_hint.get("page_url_template") or "",
            "max_pages": 1,
            "list_item_selector": html_hint.get("list_item_selector") or "",
            "detail_link_selector": html_hint.get("detail_link_selector") or "a",
            "title_selector": html_hint.get("title_selector") or "h1",
            "date_selector": html_hint.get("date_selector") or DEFAULT_DATE_SELECTOR,
            "tag_selector": html_hint.get("tag_selector") or DEFAULT_TAG_SELECTOR,
            "body_selector": html_hint.get("body_selector") or DEFAULT_BODY_SELECTOR,
            "media_selector": html_hint.get("media_selector") or DEFAULT_MEDIA_SELECTOR,
            "skip_head_images": 0,
            "skip_tail_images": 0,
            "enabled": True,
            "start_date": "",
            "confidence": int(html_hint.get("confidence") or 30),
            "message": html_hint.get("message") or "已按 HTML 列表页生成建议",
            "preview": html_hint.get("preview") or [],
        }

    def preview(self, source: dict[str, Any], limit: int = 3) -> list[ParsedPost]:
        return self.discover(source, limit=limit, parse_assets=False)

    def discover(self, source: dict[str, Any], limit: int | None = None, parse_assets: bool = True) -> list[ParsedPost]:
        source_type = str(source.get("source_type") or "html").lower()
        if source_type == "xhs_likes":
            raise ValueError("小红书点赞源不能使用通用 HTML/RSS 解析器")
        if source_type == "rss":
            posts = self._discover_rss(source, parse_assets=parse_assets, limit=limit)
        elif source_type == "sitemap":
            posts = self._discover_sitemap(source, parse_assets=parse_assets, limit=limit)
        else:
            posts = self._discover_html(source, parse_assets=parse_assets, limit=limit)
        return posts

    def _discover_html(self, source: dict[str, Any], parse_assets: bool, limit: int | None = None) -> list[ParsedPost]:
        posts: list[ParsedPost] = []
        seen: set[str] = set()
        max_pages = max(int(source.get("max_pages") or 1), 1)
        for page in range(1, max_pages + 1):
            page_url = self._page_url(source, page)
            try:
                soup = parse_html(self.fetcher.get_text(page_url))
            except Exception:
                if page > 1:
                    break
                raise
            item_selector = source.get("list_item_selector")
            items = soup.select(item_selector) if item_selector else []
            if items:
                page_posts = 0
                for item in items:
                    detail_url = self._detail_url(item, page_url, source)
                    if not detail_url:
                        continue
                    normalized_url = detail_url.split("#", 1)[0]
                    if normalized_url in seen:
                        continue
                    seen.add(normalized_url)
                    try:
                        posts.append(self.parse_detail(normalized_url, source, parse_assets=parse_assets, fallback_node=item))
                    except Exception:
                        continue
                    page_posts += 1
                    if limit and len(posts) >= limit:
                        return posts
                if page > 1 and page_posts == 0:
                    break
                continue

            page_posts = 0
            for detail_url in self._sniff_detail_urls(soup, page_url):
                normalized_url = detail_url.split("#", 1)[0]
                if normalized_url in seen:
                    continue
                seen.add(normalized_url)
                try:
                    posts.append(self.parse_detail(normalized_url, source, parse_assets=parse_assets))
                except Exception:
                    continue
                page_posts += 1
                if limit and len(posts) >= limit:
                    return posts
            if page > 1 and page_posts == 0:
                break
        return posts

    def _discover_rss(self, source: dict[str, Any], parse_assets: bool, limit: int | None = None) -> list[ParsedPost]:
        root = ElementTree.fromstring(self.fetcher.get_text(source["entry_url"]).encode("utf-8"))
        items = [node for node in root.iter() if self._xml_name(node.tag) in {"item", "entry"}]
        posts: list[ParsedPost] = []
        for item in items:
            title = self._xml_child_text(item, "title") or "未命名贴文"
            link = self._rss_link(item)
            if not link:
                continue
            pub_date = self._first_date(
                self._xml_child_text(item, "pubDate"),
                self._xml_child_text(item, "published"),
                self._xml_child_text(item, "updated"),
            )
            tags = [node.text.strip() for node in item.iter() if self._xml_name(node.tag) == "category" and node.text]
            excerpt = self._xml_child_text(item, "description") or self._xml_child_text(item, "summary") or ""
            assets = self._rss_assets(item, link)
            if parse_assets and source.get("media_selector"):
                detail = self.parse_detail(link, source, parse_assets=True)
                assets.extend(detail.assets)
                title = detail.title or title
                pub_date = detail.pub_date or pub_date
                tags = detail.tags or tags
                excerpt = detail.excerpt or excerpt
            posts.append(ParsedPost(link, title, pub_date, [tag for tag in tags if tag], excerpt, self._dedupe_assets(assets)))
            if limit and len(posts) >= limit:
                return self._dedupe_posts(posts)
        return self._dedupe_posts(posts)

    def _discover_sitemap(self, source: dict[str, Any], parse_assets: bool, limit: int | None = None) -> list[ParsedPost]:
        root = ElementTree.fromstring(self.fetcher.get_text(source["entry_url"]).encode("utf-8"))
        posts: list[ParsedPost] = []
        for item in [node for node in root.iter() if self._xml_name(node.tag) == "url"]:
            loc = self._xml_child_text(item, "loc")
            if not loc:
                continue
            lastmod = self._first_date(self._xml_child_text(item, "lastmod"))
            detail = self.parse_detail(loc, source, parse_assets=parse_assets)
            if not detail.pub_date:
                detail.pub_date = lastmod
            posts.append(detail)
            if limit and len(posts) >= limit:
                return self._dedupe_posts(posts)
        return self._dedupe_posts(posts)

    def parse_detail(self, url: str, source: dict[str, Any], parse_assets: bool = True, fallback_node: Any | None = None) -> ParsedPost:
        text = self.fetcher.get_text(url)
        soup = parse_html(text)
        title = self._selector_text(soup, source.get("title_selector"))
        if not title and fallback_node:
            title = self._selector_text(fallback_node, source.get("title_selector"))
        if not title:
            title = self._selector_text(soup, "h1") or self._selector_text(soup, "title") or "未命名贴文"
        pub_date = self._selector_date(soup, source.get("date_selector"))
        if not pub_date and fallback_node:
            pub_date = self._selector_date(fallback_node, source.get("date_selector"))
        if not pub_date and fallback_node:
            pub_date = self._node_asset_date_hint(fallback_node) or self._first_date_in_text(fallback_node.get_text(" ", strip=True))
        if not pub_date:
            pub_date = self._html_date_fallback(soup, text)
        tags = self._selector_texts(soup, source.get("tag_selector"))
        if not tags and fallback_node:
            tags = self._selector_texts(fallback_node, source.get("tag_selector"))
        excerpt = self._selector_text(soup, source.get("body_selector")) or ""
        assets = self._media_assets(soup, source.get("media_selector"), url) if parse_assets else []
        if self._looks_like_gallery_epic(url, text):
            if not title or title == "未命名贴文":
                title = self._gallery_epic_title(text, soup)
            pub_date = pub_date or self._gallery_epic_date(text)
            if parse_assets:
                gallery_assets = self._gallery_epic_assets(text)
                if gallery_assets:
                    assets = gallery_assets
                else:
                    assets = [asset for asset in assets if "galleryepic" not in urlparse(asset.url).netloc.lower()]
        if not pub_date and assets:
            pub_date = self._asset_date_fallback(assets)
        return ParsedPost(url=url, title=title.strip(), pub_date=pub_date, tags=tags, excerpt=excerpt.strip(), assets=assets)

    def _looks_like_gallery_epic(self, url: str, text: str) -> bool:
        host = urlparse(url).netloc.lower()
        return "galleryepic." in host or "static.galleryepic.xyz/image/" in text or "Gallery Epic" in text

    def _gallery_epic_title(self, text: str, soup: Any) -> str:
        title = self._selector_text(soup, "h1") or self._selector_text(soup, "title")
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return re.split(r"\s*[|｜]\s*", title, maxsplit=1)[0].strip() or title
        for key in ("character", "characterEnglish", "name", "nameEnglish"):
            match = re.search(rf'\\"{key}\\":\\"([^\\"]+)\\"', text) or re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
            if match:
                return match.group(1).strip()
        return "Gallery Epic"

    def _gallery_epic_date(self, text: str) -> str | None:
        for pattern in (
            r'\\"createdAt\\":\\"\$D([^\\"]+)\\"',
            r'"createdAt"\s*:\s*"\$D([^"]+)"',
            r'\\"updatedAt\\":\\"\$D([^\\"]+)\\"',
            r'"updatedAt"\s*:\s*"\$D([^"]+)"',
        ):
            match = re.search(pattern, text)
            if not match:
                continue
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed.isoformat()
        return None

    def _gallery_epic_assets(self, text: str) -> list[ParsedAsset]:
        serialized_image_ids = self._gallery_epic_serialized_image_ids(text)
        if serialized_image_ids:
            static_origin_match = re.search(
                r"(https://static\.galleryepic\.[A-Za-z.]+/image)/[A-Za-z0-9_-]+",
                text,
            )
            static_origin = static_origin_match.group(1) if static_origin_match else "https://static.galleryepic.xyz/image"
            return [ParsedAsset(f"{static_origin}/{image_id}", "image") for image_id in serialized_image_ids]

        cutoff_candidates = [
            text.find('href="/zh/cosplay/'),
            text.find('\\"href\\":\\"/zh/cosplay/'),
        ]
        cutoff_candidates = [index for index in cutoff_candidates if index > 0]
        detail_text = text[: min(cutoff_candidates)] if cutoff_candidates else text
        urls = re.findall(r"https://static\.galleryepic\.xyz/image/[A-Za-z0-9-]+", detail_text)
        if len(urls) < 2:
            urls = re.findall(r"https://static\.galleryepic\.xyz/image/[A-Za-z0-9-]+", text)
        assets = []
        for url in urls:
            path = urlparse(url).path.lower().rstrip("/")
            if path == "/image/avatar" or path.startswith("/image/avatar/"):
                continue
            assets.append(ParsedAsset(url, "image"))
        return assets

    def _gallery_epic_serialized_image_ids(self, text: str) -> list[str]:
        image_lists: list[list[str]] = []
        pattern = re.compile(
            r"(?:\\+)?[\"']images(?:\\+)?[\"']\s*:\s*(?:\\+)?[\"'](?P<images>\[.*?\])(?:\\+)?[\"']",
            re.DOTALL,
        )
        for match in pattern.finditer(text):
            image_ids = re.findall(
                r"(?<![A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9_-]{7,})(?![A-Za-z0-9_-])",
                match.group("images"),
            )
            if image_ids:
                image_lists.append(list(dict.fromkeys(image_ids)))
        return max(image_lists, key=len, default=[])

    def _xml_source_hint(self, text: str) -> tuple[str | None, str | None]:
        try:
            root = ElementTree.fromstring(text.encode("utf-8"))
        except ElementTree.ParseError:
            return None, None
        root_name = self._xml_name(root.tag).lower()
        if root_name in {"rss", "feed"}:
            return "rss", self._xml_child_text(root, "title") or None
        if root_name in {"urlset", "sitemapindex"}:
            return "sitemap", self._xml_child_text(root, "loc") or None
        return None, None

    def _html_site_name(self, soup: Any, entry_url: str) -> str:
        title = self._selector_text(soup, "title") or self._selector_text(soup, "h1")
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            name = re.split(r"\s*[|｜]\s*|\s+[-–—]\s+|(?<=[\u4e00-\u9fff])[-–—]\s*|(?<=[A-Za-z0-9.)])[-–—](?=[A-Z\u4e00-\u9fff])", title, maxsplit=1)[0].strip()
            return name or title
        return self._url_site_name(entry_url)

    def _url_site_name(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc:
            return parsed.netloc.removeprefix("www.")
        name = Path(parsed.path).stem or Path(parsed.path).parent.name
        return name or "未命名来源"

    def _html_source_hint(self, soup: Any, entry_url: str) -> dict[str, Any]:
        candidates = []
        for selector in [
            "a[href*='/cosplay/']",
            ".library-list .library-item",
            ".library-list li",
            ".library-item",
            ".library-card",
            ".gallery-list .gallery-item",
            ".gallery-item",
            ".content-post",
            ".post-list",
            ".post-card",
            ".post-item",
            ".posts-item",
            ".grid-item",
            ".ajax-item",
            ".i_list",
            ".update_area_lists li",
            ".cxudy-list-formatimage",
            "article",
            ".hentry",
            ".entry",
            ".post",
            ".article",
            ".item",
            "[role='article']",
            "li",
        ]:
            try:
                nodes = soup.select(selector)
            except Exception:
                continue
            score, preview = self._score_list_selector(selector, nodes, entry_url)
            if score > 0:
                candidates.append((score, selector, preview))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        if candidates:
            score, selector, preview = candidates[0]
            return {
                "list_item_selector": selector,
                "detail_link_selector": "a",
                "title_selector": "h1",
                "date_selector": DEFAULT_DATE_SELECTOR,
                "tag_selector": DEFAULT_TAG_SELECTOR,
                "body_selector": DEFAULT_BODY_SELECTOR,
                "media_selector": DEFAULT_MEDIA_SELECTOR,
                "page_url_template": self._guess_page_url_template(soup, entry_url),
                "confidence": min(95, max(40, score)),
                "message": f"已识别列表项选择器 {selector}",
                "preview": preview,
            }
        return {
            "list_item_selector": "",
            "detail_link_selector": "a",
            "title_selector": "h1",
            "date_selector": DEFAULT_DATE_SELECTOR,
            "tag_selector": DEFAULT_TAG_SELECTOR,
            "body_selector": DEFAULT_BODY_SELECTOR,
            "media_selector": DEFAULT_MEDIA_SELECTOR,
            "page_url_template": self._guess_page_url_template(soup, entry_url),
            "confidence": 25,
            "message": "未找到稳定列表项，已保留自动嗅探详情链接",
            "preview": [],
        }

    def _score_list_selector(self, selector: str, nodes: list[Any], base_url: str) -> tuple[int, list[dict[str, str]]]:
        if len(nodes) < 2:
            return 0, []
        score = 0
        preview: list[dict[str, str]] = []
        valid_links = 0
        dated = 0
        titled = 0
        media = 0
        for node in nodes[:30]:
            detail_url = self._first_detail_url(node, base_url)
            if not detail_url:
                continue
            valid_links += 1
            title = self._node_title_hint(node)
            pub_date = self._selector_date(node, DEFAULT_DATE_SELECTOR)
            if not pub_date:
                pub_date = self._node_asset_date_hint(node)
            if title:
                titled += 1
            if pub_date:
                dated += 1
            if self._node_has_media(node):
                media += 1
            if len(preview) < 3:
                preview.append({"url": detail_url, "title": title or detail_url, "pub_date": pub_date or ""})
        if valid_links < 2:
            return 0, []
        score += valid_links * 5
        score += dated * 4
        score += titled * 2
        score += media
        if selector == "li" and dated == 0 and media < max(2, valid_links // 3):
            return 0, []
        if selector in {
            ".library-list .library-item",
            ".library-list li",
            ".library-item",
            ".library-card",
            ".gallery-list .gallery-item",
            ".gallery-item",
            ".content-post",
            ".post-list",
            ".post-card",
            ".post-item",
            ".posts-item",
            ".grid-item",
            ".ajax-item",
            ".i_list",
            ".update_area_lists li",
            ".cxudy-list-formatimage",
            "article",
            ".hentry",
            ".post",
            ".item",
        }:
            score += 18
        elif "." in selector or "[" in selector:
            score += 8
        if selector == "li":
            score -= 45
        if len(nodes) > 60:
            score -= min(35, len(nodes) - 60)
        return max(score, 0), preview

    def _first_detail_url(self, node: Any, base_url: str) -> str | None:
        links = []
        if getattr(node, "name", "") == "a" and node.get("href"):
            links.append(node)
        try:
            links.extend(node.select("a[href]")[:8])
        except Exception:
            return None
        for link in links:
            href = link.get("href")
            if not href:
                continue
            detail_url = urljoin(base_url, href).split("#", 1)[0]
            if self._detail_url_score(base_url, detail_url, link.get_text(" ", strip=True)) > 0:
                return detail_url
        return None

    def _node_title_hint(self, node: Any) -> str:
        for selector in [".library-title", ".gallery-title", ".card-title", ".item-title", ".meta-title", ".entry-title", ".post-title", ".title", "h1", "h2", "h3", "a"]:
            title = self._selector_text(node, selector)
            title = re.sub(r"\s+", " ", title).strip()
            if title and not parse_date(title):
                return title[:160]
        try:
            images = node.select("img[alt]")
        except Exception:
            images = []
        for image in images:
            title = re.sub(r"\s+", " ", str(image.get("alt") or "")).strip()
            if title and not parse_date(title):
                return title[:160]
        return ""

    def _node_has_media(self, node: Any) -> bool:
        try:
            return bool(node.select_one("img, video, source"))
        except Exception:
            return False

    def _node_asset_date_hint(self, node: Any) -> str | None:
        try:
            images = node.select("img")
        except Exception:
            return None
        for image in images:
            for key in ("data-original", "data-src", "data-lazy-src", "src"):
                value = str(image.get(key) or "")
                if pub_date := self._date_from_media_path(value):
                    return pub_date
            for key in ("data-srcset", "srcset"):
                if first_src := self._first_srcset_url(str(image.get(key) or "")):
                    if pub_date := self._date_from_media_path(first_src):
                        return pub_date
        return None

    def _guess_page_url_template(self, soup: Any, entry_url: str) -> str:
        try:
            links = soup.select("a[href]")
        except Exception:
            return ""
        parsed_entry = urlparse(entry_url)
        for link in links:
            label = link.get_text(" ", strip=True)
            href = link.get("href") or ""
            if label.strip() != "2" and not re.search(r"(^|[/?=&])2($|[/?&#])", href):
                continue
            absolute = urljoin(entry_url, href)
            parsed = urlparse(absolute)
            if parsed_entry.netloc and parsed.netloc != parsed_entry.netloc:
                continue
            if re.search(r"/page/2/?$", parsed.path):
                return absolute.replace("/page/2", "/page/{page}").rstrip("/")
            if re.search(r"/2/?$", parsed.path) and parsed.path.rstrip("/").rsplit("/", 1)[-1] == "2":
                return re.sub(r"/2/?$", "/{page}", absolute).rstrip("/")
            if "page=2" in parsed.query:
                return absolute.replace("page=2", "page={page}")
            if "paged=2" in parsed.query:
                return absolute.replace("paged=2", "paged={page}")
        return ""

    def _page_url(self, source: dict[str, Any], page: int) -> str:
        if page <= 1:
            return source["entry_url"]
        template = source.get("page_url_template")
        if template:
            return str(template).format(page=page)
        return source["entry_url"]

    def _detail_url(self, item: Any, base_url: str, source: dict[str, Any]) -> str | None:
        selector = source.get("detail_link_selector") or "a"
        link = item if self._item_matches_own_link(item, selector) else None
        if not link:
            link = item.select_one(selector)
        if not link:
            return None
        href = link.get("href") or link.get("src")
        if not href:
            nested = link.select_one("a[href]") if hasattr(link, "select_one") else None
            href = nested.get("href") if nested else None
        if not href:
            return None
        detail_url = urljoin(base_url, href).split("#", 1)[0]
        link_text = link.get_text(" ", strip=True)
        return detail_url if self._detail_url_score(base_url, detail_url, link_text) > 0 else None

    def _item_matches_own_link(self, item: Any, selector: str) -> bool:
        if getattr(item, "name", "") != "a" or not item.get("href"):
            return False
        normalized = (selector or "a").strip()
        return normalized in {"a", "a[href]"} or normalized.startswith("a[")

    def _sniff_detail_urls(self, soup: Any, base_url: str) -> list[str]:
        candidates: list[tuple[int, str]] = []
        for link in soup.select("a[href]"):
            href = link.get("href")
            if not href:
                continue
            absolute = urljoin(base_url, href)
            score = self._detail_url_score(base_url, absolute, link.get_text(" ", strip=True))
            if score <= 0:
                continue
            candidates.append((score, absolute.split("#", 1)[0]))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        output: list[str] = []
        seen: set[str] = set()
        for _score, url in candidates:
            if url in seen:
                continue
            seen.add(url)
            output.append(url)
        return output

    def _detail_url_score(self, base_url: str, candidate_url: str, link_text: str) -> int:
        parsed_base = urlparse(base_url)
        parsed_candidate = urlparse(candidate_url)
        if parsed_candidate.scheme not in {"http", "https", "file"}:
            return 0
        if parsed_base.scheme == "file" and parsed_candidate.scheme != "file":
            return 0
        if parsed_base.scheme in {"http", "https"} and parsed_base.netloc != parsed_candidate.netloc:
            return 0
        if parsed_candidate.path in {"", "/"} and not parsed_candidate.query:
            return 0
        if parsed_candidate.path == parsed_base.path and not parsed_candidate.query:
            return 0
        suffix = Path(parsed_candidate.path.lower()).suffix
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov", ".zip", ".rar", ".7z", ".pdf"}:
            return 0

        path = parsed_candidate.path.lower()
        if any(token in path for token in ("/download/", "/downloads/")):
            return 0
        text = (link_text or "").strip()
        score = 1
        if text:
            score += 1
        if any(token in path for token in ("/post", "/posts", "/article", "/entry", "/archives", "/blog", "/news", "/library", "/gallery", "/photo")):
            score += 4
        if any(token in path for token in ("/20", "date=", "p=", "id=")):
            score += 2
        if len([part for part in path.split("/") if part]) >= 2:
            score += 1
        if any(token in path for token in ("/tag", "/category", "/author", "/page", "/login", "/contact", "/about", "/privacy")):
            score -= 3
        return max(score, 0)

    def _selector_text(self, soup: Any | None, selector: str | None) -> str:
        if not soup or not selector:
            return ""
        try:
            nodes = soup.select(selector)
        except Exception:
            return ""
        for node in nodes:
            text = self._node_text(node)
            if text:
                return text
        return ""

    def _selector_texts(self, soup: Any | None, selector: str | None) -> list[str]:
        if not soup or not selector:
            return []
        return [node.get_text(" ", strip=True) for node in soup.select(selector) if node.get_text(" ", strip=True)]

    def _selector_date(self, soup: Any | None, selector: str | None) -> str | None:
        if not soup or not selector:
            return None
        try:
            nodes = soup.select(selector)
        except Exception:
            return None
        for node in nodes:
            for value in self._node_date_values(node):
                parsed = parse_date(value)
                if parsed:
                    return parsed.isoformat()
        return None

    def _node_text(self, node: Any) -> str:
        for key in ("content", "datetime", "title", "aria-label"):
            value = node.get(key)
            if value:
                return str(value).strip()
        return node.get_text(" ", strip=True)

    def _node_date_values(self, node: Any) -> list[str]:
        values = []
        for key in ("datetime", "content", "title", "aria-label", "data-date", "data-time", "data-published"):
            value = node.get(key)
            if value:
                values.append(str(value))
        text = node.get_text(" ", strip=True)
        if text:
            values.append(text)
        return values

    def _html_date_fallback(self, soup: Any, text: str) -> str | None:
        for node in self._iter_nodes(soup, "meta"):
            key = str(node.get("property") or node.get("name") or node.get("itemprop") or "").lower()
            if key not in {
                "article:published_time",
                "article:modified_time",
                "date",
                "datepublished",
                "datemodified",
                "pubdate",
                "publishdate",
                "dc.date",
                "dc.date.issued",
                "og:updated_time",
            }:
                continue
            parsed = parse_date(str(node.get("content") or ""))
            if parsed:
                return parsed.isoformat()
        for selector in (
            "time",
            ".entry-meta",
            ".post-meta",
            ".posted-on",
            ".published",
            ".updated",
            ".post-date",
            ".date",
            ".meta-date",
            ".image-info-time",
            ".item_images_info",
        ):
            pub_date = self._selector_date(soup, selector)
            if pub_date:
                return pub_date
        for key in ("datePublished", "dateModified", "uploadDate"):
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
            if match:
                parsed = parse_date(match.group(1))
                if parsed:
                    return parsed.isoformat()
        for selector in ("article", "main", ".post", ".entry-content", ".post-content", ".content"):
            try:
                nodes = soup.select(selector)
            except Exception:
                continue
            for node in nodes[:3]:
                pub_date = self._first_date_in_text(node.get_text(" ", strip=True))
                if pub_date:
                    return pub_date
        return None

    def _first_date_in_text(self, text: str) -> str | None:
        for match in re.finditer(r"\b(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})", text):
            parsed = parse_date(match.group(0))
            if parsed:
                return parsed.isoformat()
        return None

    def _asset_date_fallback(self, assets: list[ParsedAsset]) -> str | None:
        for asset in assets:
            if pub_date := self._date_from_media_path(asset.url):
                return pub_date
        return None

    def _date_from_media_path(self, value: str) -> str | None:
        path = urlparse(str(value or "")).path
        match = re.search(r"(?:^|/)(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)", path)
        if not match:
            return None
        parsed = parse_date(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
        return parsed.isoformat() if parsed else None

    def _iter_nodes(self, root: Any, name: str | None = None) -> list[Any]:
        if hasattr(root, "find_all"):
            return list(root.find_all(name or True))
        output: list[Any] = []

        def visit(node: Any) -> None:
            for child in getattr(node, "children", []) or []:
                if name is None or getattr(child, "name", None) == name:
                    output.append(child)
                visit(child)

        visit(root)
        return output

    def _media_assets(self, soup: Any, selector: str | None, base_url: str) -> list[ParsedAsset]:
        if not selector:
            selector = "img, video, source, a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp'], a[href$='.gif'], a[href$='.mp4'], a[href$='.webm'], a[href$='.mov']"
        assets: list[ParsedAsset] = []
        selected_nodes = soup.select(selector)
        if hasattr(soup, "find_all"):
            selected_ids = {id(node) for node in selected_nodes}
            selected_nodes = [node for node in soup.find_all(True) if id(node) in selected_ids]
        elif hasattr(soup, "_descendants"):
            selected_ids = {id(node) for node in selected_nodes}
            selected_nodes = [node for node in soup._descendants() if id(node) in selected_ids]
        for node in selected_nodes:
            url = self._media_url(node)
            if not url:
                continue
            media_url = urljoin(base_url, url)
            media_type = "video" if node.name in {"video", "source"} or self._is_video_url(media_url) else "image"
            if media_type in {"image", "video"}:
                assets.append(ParsedAsset(media_url, media_type))
        return self._dedupe_assets(assets)

    def _media_url(self, node: Any) -> str:
        values = [
            node.get("data-src"),
            node.get("data-original"),
            node.get("data-lazy-src"),
            node.get("src"),
            node.get("href"),
            node.get("poster"),
        ]
        for value in values:
            url = str(value or "").strip()
            if not url or url.startswith("data:") or url.startswith("about:"):
                continue
            return url
        for key in ("data-srcset", "srcset"):
            if first_src := self._first_srcset_url(str(node.get(key) or "")):
                return first_src
        return ""

    def _first_srcset_url(self, srcset: str) -> str | None:
        for part in srcset.split(","):
            url = part.strip().split(" ", 1)[0].strip()
            if url and not url.startswith("data:"):
                return url
        return None

    def _rss_link(self, item: Any) -> str:
        for child in item.iter():
            if self._xml_name(child.tag) == "link":
                return child.attrib.get("href") or (child.text or "").strip()
        return ""

    def _rss_assets(self, item: Any, base_url: str) -> list[ParsedAsset]:
        assets: list[ParsedAsset] = []
        for node in item.iter():
            if self._xml_name(node.tag) not in {"enclosure", "content"}:
                continue
            url = node.attrib.get("url") or node.attrib.get("src")
            if not url:
                continue
            media_type_hint = (node.attrib.get("type") or "").lower()
            media_url = urljoin(base_url, url)
            media_type = "video" if "video" in media_type_hint or self._is_video_url(media_url) else "image"
            assets.append(ParsedAsset(media_url, media_type))
        return self._dedupe_assets(assets)

    def _first_date(self, *values: str | None) -> str | None:
        for value in values:
            parsed = parse_date(value)
            if parsed:
                return parsed.isoformat()
        return None

    def _xml_child_text(self, item: Any, name: str) -> str:
        for child in item.iter():
            if self._xml_name(child.tag) == name and child.text:
                return child.text.strip()
        return ""

    def _xml_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1].split(":")[-1]

    def _dedupe_posts(self, posts: list[ParsedPost]) -> list[ParsedPost]:
        output: list[ParsedPost] = []
        seen = set()
        for post in posts:
            if post.url in seen:
                continue
            seen.add(post.url)
            output.append(post)
        return output

    def _dedupe_assets(self, assets: list[ParsedAsset]) -> list[ParsedAsset]:
        output: list[ParsedAsset] = []
        seen = set()
        for asset in assets:
            key = asset.url.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            output.append(asset)
        return output

    def _is_video_url(self, url: str) -> bool:
        return Path(urlparse(url).path.lower()).suffix in {".mp4", ".webm", ".mov", ".m4v"}
