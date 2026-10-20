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
    def __init__(self, category, diagnostic=None):
        super().__init__(category)
        self.diagnostic = diagnostic if isinstance(diagnostic, dict) else {}


def public_target(url: str) -> tuple[str, list[str]]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not any(host == domain or host.endswith('.' + domain) for domain in ('xhscdn.com', 'xiaohongshu.com'))):
            raise MediaRejected('MEDIA_HOST_REJECTED', {'host': host[:253]})
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise MediaRejected('MEDIA_ADDRESS_REJECTED', {'host': host[:253]})
        # Try IPv4 first: container networks frequently have no IPv6 route even
        # when the resolver still hands out AAAA records.
        return host, sorted(addresses, key=lambda address: (':' in address, address))
    except MediaRejected:
        raise
    except (ValueError, OSError):
        raise MediaRejected('MEDIA_ADDRESS_REJECTED', {'host': (urlsplit(url).hostname or '')[:253]}) from None


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float = 30.0):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def download_public_media(url: str, target: Path, *, max_bytes: int = 512 * 1024 * 1024,
                          cancelled=None, timeout: float = 300.0) -> dict:
    part = target.with_suffix('.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    response = connection = None
    deadline = time.monotonic() + timeout
    stage = 'start'
    try:
        for _ in range(5):
            stage = 'resolve'
            if cancelled and cancelled():
                raise MediaRejected('MEDIA_CANCELLED')
            if time.monotonic() > deadline:
                raise MediaRejected('MEDIA_TIME_LIMIT', {'stage': stage})
            host, addresses = public_target(url)
            parsed = urlsplit(url)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            stage = 'connect'
            connection = response = None
            last_error = None
            for address in addresses:
                if time.monotonic() > deadline:
                    break
                try:
                    # Give a stalled stream more patience, but never exceed the
                    # candidate budget by more than a few seconds.
                    socket_timeout = max(10.0, min(60.0, deadline - time.monotonic()))
                    connection = PinnedConnection(host, address, timeout=socket_timeout)
                    connection.request('GET', path, headers={
                        'Host': host, 'Referer': 'https://www.xiaohongshu.com/',
                        'Accept': 'image/*,video/mp4'})
                    stage = 'response'
                    response = connection.getresponse()
                    break
                except (OSError, http.client.HTTPException) as exc:
                    last_error = exc
                    if connection:
                        connection.close()
                    connection = None
                    continue
            if response is None:
                if time.monotonic() > deadline:
                    raise MediaRejected('MEDIA_TIME_LIMIT', {'stage': 'connect', 'host': host})
                raise last_error if last_error else MediaRejected('MEDIA_ADDRESS_REJECTED', {'host': host})
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location', '')
                response.close()
                connection.close()
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise MediaRejected('MEDIA_UNAVAILABLE', {'status': response.status, 'host': host})
            break
        else:
            raise MediaRejected('MEDIA_REDIRECT_LIMIT')
        stage = 'validate'
        mime = response.getheader('Content-Type', '').split(';')[0].lower()
        if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif', 'video/mp4'}:
            raise MediaRejected('MEDIA_TYPE_REJECTED', {'mime': mime[:64], 'host': host})
        length = int(response.getheader('Content-Length', '0'))
        if length > max_bytes or shutil.disk_usage(target.parent).free < max(length, 8 * 1024 * 1024) + 128 * 1024 * 1024:
            raise MediaRejected('MEDIA_QUOTA_EXCEEDED')
        stage = 'download'
        digest, total = hashlib.sha256(), 0
        with part.open('wb') as output:
            while chunk := response.read(64 * 1024):
                if cancelled and cancelled():
                    raise MediaRejected('MEDIA_CANCELLED')
                if time.monotonic() > deadline:
                    raise MediaRejected('MEDIA_TIME_LIMIT', {'stage': stage})
                total += len(chunk)
                if total > max_bytes or shutil.disk_usage(target.parent).free < 128 * 1024 * 1024:
                    raise MediaRejected('MEDIA_QUOTA_EXCEEDED')
                digest.update(chunk)
                output.write(chunk)
        if total == 0 or (length and total != length):
            raise MediaRejected('MEDIA_INCOMPLETE', {'expected': length, 'actual': total, 'host': host})
        width = height = 0
        duration = 0.0
        if mime.startswith('image/'):
            stage = 'verify'
            with Image.open(part) as image:
                width, height = image.size
                if width * height > 100000000:
                    raise MediaRejected('MEDIA_DIMENSIONS_REJECTED')
                image.verify()
        else:
            stage = 'verify'
            with part.open('rb') as media_file:
                header = media_file.read(32)
            if b'ftyp' not in header:
                raise MediaRejected('MEDIA_MAGIC_REJECTED')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaRejected('MEDIA_TIME_LIMIT', {'stage': stage})
            stage = 'probe'
            try:
                import imageio_ffmpeg
                executable = imageio_ffmpeg.get_ffmpeg_exe()
            except (ImportError, OSError, RuntimeError):
                executable = None
            probe = None
            if executable:
                try:
                    probe = subprocess.run([executable, '-hide_banner', '-i', str(part),
                                            '-frames:v', '1', '-f', 'null', '-'], capture_output=True,
                                           timeout=max(1.0, min(60.0, remaining)))
                except subprocess.TimeoutExpired:
                    raise MediaRejected('MEDIA_TIME_LIMIT', {'stage': stage}) from None
                except (OSError, subprocess.SubprocessError):
                    # Some NAS containers cannot execute the bundled ffmpeg.
                    # The ftyp magic and Content-Length already passed, so keep
                    # the video with empty metadata instead of failing the note.
                    probe = None
            if probe is not None and probe.returncode:
                raise MediaRejected('MEDIA_DECODE_REJECTED', {'returncode': probe.returncode})
            if probe is not None:
                diagnostic = probe.stderr.decode('utf-8', errors='replace')
                dimensions = re.search(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b', diagnostic)
                length_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', diagnostic)
                if dimensions:
                    width, height = map(int, dimensions.groups())
                if length_match:
                    hours, minutes, seconds = map(float, length_match.groups())
                    duration = hours * 3600 + minutes * 60 + seconds
        stage = 'finalize'
        part.replace(target)
        return {'sha256': digest.hexdigest(), 'size': total, 'mime': mime, 'width': width, 'height': height,
                'duration': duration, 'variant': 'visible_web_media'}
    except MediaRejected:
        part.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        diagnostic = {'stage': stage, 'error': type(exc).__name__}
        errno = getattr(exc, 'errno', None)
        if errno is not None:
            diagnostic['errno'] = errno
        try:
            diagnostic['host'] = (urlsplit(url).hostname or '')[:253]
        except ValueError:
            pass
        part.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise MediaRejected('MEDIA_DOWNLOAD_FAILED', diagnostic) from None
    finally:
        if response:
            response.close()
        if connection:
            connection.close()
