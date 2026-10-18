from __future__ import annotations

import hashlib
import http.client
import ipaddress
import shutil
import socket
import ssl
import subprocess
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image


class MediaRejected(ValueError):
    pass


def public_target(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not any(host == domain or host.endswith('.' + domain) for domain in ('xhscdn.com', 'xiaohongshu.com'))):
            raise MediaRejected('MEDIA_HOST_REJECTED')
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise MediaRejected('MEDIA_ADDRESS_REJECTED')
        return host, sorted(addresses)[0]
    except (ValueError, OSError):
        raise MediaRejected('MEDIA_ADDRESS_REJECTED') from None


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=30, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def download_public_media(url: str, target: Path, *, max_bytes: int = 512 * 1024 * 1024, cancelled=None) -> dict:
    part = target.with_suffix('.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    response = connection = None
    deadline = time.monotonic() + 300
    try:
        for _ in range(5):
            host, address = public_target(url)
            parsed = urlsplit(url)
            connection = PinnedConnection(host, address)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={'Host': host, 'Referer': 'https://www.xiaohongshu.com/', 'Accept': 'image/*,video/mp4'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location', '')
                response.close()
                connection.close()
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise MediaRejected('MEDIA_UNAVAILABLE')
            break
        else:
            raise MediaRejected('MEDIA_REDIRECT_LIMIT')
        mime = response.getheader('Content-Type', '').split(';')[0].lower()
        if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif', 'video/mp4'}:
            raise MediaRejected('MEDIA_TYPE_REJECTED')
        length = int(response.getheader('Content-Length', '0'))
        if length > max_bytes or shutil.disk_usage(target.parent).free < max(length, 8 * 1024 * 1024) + 128 * 1024 * 1024:
            raise MediaRejected('MEDIA_QUOTA_EXCEEDED')
        digest, total = hashlib.sha256(), 0
        with part.open('wb') as output:
            while chunk := response.read(64 * 1024):
                if cancelled and cancelled():
                    raise MediaRejected('MEDIA_CANCELLED')
                if time.monotonic() > deadline:
                    raise MediaRejected('MEDIA_TIME_LIMIT')
                total += len(chunk)
                if total > max_bytes or shutil.disk_usage(target.parent).free < 128 * 1024 * 1024:
                    raise MediaRejected('MEDIA_QUOTA_EXCEEDED')
                digest.update(chunk)
                output.write(chunk)
        if total == 0 or (length and total != length):
            raise MediaRejected('MEDIA_INCOMPLETE')
        width = height = 0
        duration = 0.0
        if mime.startswith('image/'):
            with Image.open(part) as image:
                width, height = image.size
                if width * height > 100000000:
                    raise MediaRejected('MEDIA_DIMENSIONS_REJECTED')
                image.verify()
        else:
            with part.open('rb') as media_file:
                header = media_file.read(32)
            if b'ftyp' not in header:
                raise MediaRejected('MEDIA_MAGIC_REJECTED')
            import imageio_ffmpeg
            probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-i', str(part),
                                    '-frames:v', '1', '-f', 'null', '-'], capture_output=True, timeout=60)
            if probe.returncode:
                raise MediaRejected('MEDIA_DECODE_REJECTED')
            diagnostic = probe.stderr.decode('utf-8', errors='replace')
            dimensions = re.search(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b', diagnostic)
            length_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', diagnostic)
            if dimensions:
                width, height = map(int, dimensions.groups())
            if length_match:
                hours, minutes, seconds = map(float, length_match.groups())
                duration = hours * 3600 + minutes * 60 + seconds
        part.replace(target)
        return {'sha256': digest.hexdigest(), 'size': total, 'mime': mime, 'width': width, 'height': height,
                'duration': duration, 'variant': 'visible_web_media'}
    except Exception as exc:
        part.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise MediaRejected(str(exc) if isinstance(exc, MediaRejected) else 'MEDIA_DOWNLOAD_FAILED') from None
    finally:
        if response:
            response.close()
        if connection:
            connection.close()
