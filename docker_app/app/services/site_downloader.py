from __future__ import annotations
from app.services.database_safety import durable_replace

from pathlib import Path
from urllib.parse import urlparse

import requests

from app.services.site_parser import DEFAULT_SITE_USER_AGENT, browser_like_site_headers, site_request_timeout


class MediaDownloader:
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

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.with_suffix(f"{target.suffix}.part")
        if temp_target.exists():
            temp_target.unlink(missing_ok=True)
        if url.startswith("file://"):
            source = Path(urlparse(url).path)
            temp_target.write_bytes(source.read_bytes())
            durable_replace(temp_target, target)
            return
        try:
            with self.session.get(url, timeout=self.timeout, stream=True) as response:
                response.raise_for_status()
                with temp_target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            handle.write(chunk)
            durable_replace(temp_target, target)
        except Exception:
            temp_target.unlink(missing_ok=True)
            raise
