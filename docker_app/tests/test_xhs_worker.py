import hashlib
from io import BytesIO
import json
import socket
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from xhs_worker import media, parser
from xhs_worker.media import MediaRejected


def address_records(addresses):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET,
         socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        for address in addresses
    ]


class FakeResponse:
    def __init__(self, *, status=200, headers=None, body=b"", chunks=None):
        self.status = status
        self.headers = {key.lower(): str(value) for key, value in (headers or {}).items()}
        self.chunks = list(chunks) if chunks is not None else [body]
        self.read_calls = 0
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read(self, size=-1):
        self.read_calls += 1
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if size >= 0 and len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, host, address, response):
        self.host = host
        self.address = address
        self.response = response
        self.request_args = None
        self.closed = False

    def request(self, method, path, headers=None):
        self.request_args = (method, path, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def install_fake_connections(monkeypatch, responses, *, free_bytes=1 << 40):
    created = []

    def make_connection(host, address):
        if len(created) >= len(responses):
            raise AssertionError("unexpected connection")
        connection = FakeConnection(host, address, responses[len(created)])
        created.append(connection)
        return connection

    monkeypatch.setattr(media, "PinnedConnection", make_connection)
    monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=free_bytes))
    return created


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (3, 2), color=(12, 34, 56)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(("raw", "expected"), [
    ({"id": "top-id", "note_card": {"id": "card-id"}}, "top-id"),
    ({"note_id": "top-note-id", "note_card": {"id": "card-id"}}, "top-note-id"),
    ({"note_card": {"note_id": "nested-id"}}, "nested-id"),
    ({"noteCard": {"noteId": "camel-id"}}, "camel-id"),
    ({"note_card": {"id": "card-id"}}, "card-id"),
])
def test_normalize_note_uses_supported_identity_fields(raw, expected):
    note, candidates = parser.normalize_note(raw)

    assert note["note_id"] == expected
    assert note["url"] == f"https://www.xiaohongshu.com/explore/{expected}"
    assert candidates == []


def test_normalize_note_maps_author_time_and_cleans_title_controls():
    note, _ = parser.normalize_note({
        "id": "note_42",
        "note_card": {
            "user": {"userId": "author_9"},
            "publish_time": 1_700_000_000_000,
            "display_title": "line\nwith\tcontrols\x00",
        },
    })

    assert note["author_id"] == "author_9"
    assert note["title"] == "line with controls "
    assert note["published_at"].startswith("2023-11-15T06:13:20+08:00")
    assert note["observed_at"]


def test_normalize_note_keeps_signed_media_url_out_of_dto():
    signed_url = "https://img.xhscdn.com/image.jpg?xsec_token=private-value&sign=private-signature"
    note, candidates = parser.normalize_note({
        "id": "note_signed",
        "note_card": {"image_list": [{"url_default": signed_url}]},
    })

    assert note["assets"] == [{"ordinal": 0, "kind": "image"}]
    assert candidates == [(0, "image", signed_url)]
    assert "private-value" not in json.dumps(note)
    assert "private-signature" not in json.dumps(note)
    assert set(note) == {
        "note_id", "url", "title", "author_id", "type", "published_at", "observed_at", "assets"
    }


def test_normalize_note_extracts_video_candidate_without_exposing_it_in_dto():
    signed_url = "https://sns-video.xhscdn.com/video.mp4?token=private-value"
    note, candidates = parser.normalize_note({
        "id": "video_1",
        "note_card": {
            "type": "video",
            "video": {"media": {"stream": {"h264": [{"master_url": signed_url}]}}},
        },
    })

    assert note["type"] == "video"
    assert note["assets"] == [{"ordinal": 0, "kind": "video"}]
    assert candidates == [(0, "video", signed_url)]
    assert "private-value" not in json.dumps(note)


@pytest.mark.parametrize(("live_key", "url_key"), [
    ("live_photo", "video_url"),
    ("livePhoto", "videoUrl"),
    ("video", "url"),
])
def test_normalize_note_extracts_livephoto_video_url_as_ephemeral_asset(live_key, url_key):
    motion_url = "https://sns-video.xhscdn.com/motion.mp4?xsec_token=private-value"
    image = {"url_default": "https://img.xhscdn.com/still.jpg"}
    image[live_key] = {url_key: motion_url}

    note, candidates = parser.normalize_note({"id": "live_1", "note_card": {"image_list": [image]}})

    assert note["assets"] == [{"ordinal": 0, "kind": "image"}, {"ordinal": 0, "kind": "livephoto"}]
    assert candidates == [(0, "image", image["url_default"]), (0, "livephoto", motion_url)]
    assert "private-value" not in json.dumps(note)


@pytest.mark.parametrize(("payload", "expected_end", "expected_integrity"), [
    ({"data": {"notes": [{"id": "a"}], "has_more": False}}, True, True),
    ({"data": {"items": [{"id": "a"}], "hasMore": True}}, False, True),
    ({"data": {"notes": [], "has_more": None}}, False, False),
    ({"data": {"notes": []}}, False, False),
    ({"data": {"notes": [], "has_more": 0}}, False, False),
])
def test_response_page_distinguishes_end_of_feed_from_unknown(payload, expected_end, expected_integrity):
    result = parser.response_page(payload)

    assert result is not None
    notes, end_of_feed, integrity_ok = result
    assert len(notes) == len(payload["data"].get("notes", payload["data"].get("items")))
    assert end_of_feed is expected_end
    assert integrity_ok is expected_integrity


def test_response_page_caps_a_page_and_rejects_invalid_shapes():
    result = parser.response_page({"data": {"notes": [{"id": str(i)} for i in range(105)], "has_more": True}})

    assert result is not None and len(result[0]) == 100
    assert parser.response_page({"data": {"notes": "not-a-list", "has_more": False}}) is None
    assert parser.response_page({"data": []}) is None
    assert parser.response_page({}) is None


def test_response_page_safely_rejects_malformed_raw_json():
    assert parser.response_page(b'{"data":') is None


def test_public_target_resolves_only_whitelisted_https_hosts(monkeypatch):
    calls = []

    def fake_getaddrinfo(host, port, type=0):
        calls.append((host, port, type))
        return address_records(["8.8.8.8", "1.1.1.1"])

    monkeypatch.setattr(media.socket, "getaddrinfo", fake_getaddrinfo)

    assert media.public_target("https://CDN.XHSCdn.com/path") == ("cdn.xhscdn.com", "1.1.1.1")
    assert calls == [("cdn.xhscdn.com", 443, socket.SOCK_STREAM)]


@pytest.mark.parametrize("url", [
    "http://img.xhscdn.com/image.jpg",
    "https://attacker.test/image.jpg",
    "https://www.xiaohongshu.com.attacker.test/image.jpg",
    "https://xhscdn.com.attacker.test/image.jpg",
    "https://img.xhscdn.com:8443/image.jpg",
    "https://user@img.xhscdn.com/image.jpg",
    "https://user:password@img.xhscdn.com/image.jpg",
])
def test_public_target_rejects_spoofed_hosts_ports_and_credentials(monkeypatch, url):
    monkeypatch.setattr(
        media.socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("rejected URL reached DNS")
    )

    with pytest.raises(MediaRejected, match="MEDIA_ADDRESS_REJECTED"):
        media.public_target(url)


def test_public_target_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    monkeypatch.setattr(
        media.socket, "getaddrinfo", lambda *args, **kwargs: address_records(["93.184.216.34", "10.0.0.8"])
    )

    with pytest.raises(MediaRejected, match="MEDIA_ADDRESS_REJECTED"):
        media.public_target("https://img.xhscdn.com/image.jpg")


def test_public_target_fails_closed_when_dns_resolution_fails(monkeypatch):
    def fail_dns(*args, **kwargs):
        raise OSError("resolver unavailable")

    monkeypatch.setattr(media.socket, "getaddrinfo", fail_dns)

    with pytest.raises(MediaRejected, match="MEDIA_ADDRESS_REJECTED"):
        media.public_target("https://img.xhscdn.com/image.jpg")


def test_download_valid_image_uses_validated_pinned_target_and_returns_digest(monkeypatch, tmp_path):
    content = png_bytes()
    response = FakeResponse(headers={"Content-Type": "image/png", "Content-Length": len(content)}, body=content)
    connections = install_fake_connections(monkeypatch, [response])
    monkeypatch.setattr(media, "public_target", lambda url: ("img.xhscdn.com", "93.184.216.34"))
    target = tmp_path / "archive.png"

    result = media.download_public_media("https://img.xhscdn.com/path/image.png?sig=temporary", target)

    assert {key: result[key] for key in ("sha256", "size", "mime", "width", "height", "variant")} == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mime": "image/png",
        "width": 3,
        "height": 2,
        "variant": "visible_web_media",
    }
    assert result["duration"] == 0
    assert target.read_bytes() == content
    assert connections[0].host == "img.xhscdn.com"
    assert connections[0].address == "93.184.216.34"
    assert connections[0].request_args[0:2] == ("GET", "/path/image.png?sig=temporary")
    assert connections[0].request_args[2]["Host"] == "img.xhscdn.com"
    assert response.closed and connections[0].closed
    assert not target.with_suffix(".part").exists()


@pytest.mark.parametrize(("headers", "body", "max_bytes", "free_bytes", "expected_error"), [
    ({"Content-Type": "text/html", "Content-Length": 4}, b"<h1>", 100, 1 << 40, "MEDIA_TYPE_REJECTED"),
    ({"Content-Type": "image/png", "Content-Length": 4}, b"data", 3, 1 << 40, "MEDIA_QUOTA_EXCEEDED"),
    ({"Content-Type": "image/png", "Content-Length": 10}, b"short", 100, 1 << 40, "MEDIA_INCOMPLETE"),
    ({"Content-Type": "video/mp4", "Content-Length": 8}, b"not-mp4!", 100, 1 << 40, "MEDIA_MAGIC_REJECTED"),
    ({"Content-Type": "image/png", "Content-Length": 4}, b"data", 100, 1, "MEDIA_QUOTA_EXCEEDED"),
])
def test_download_rejects_bad_type_lengths_magic_and_quota_and_cleans_files(
    monkeypatch, tmp_path, headers, body, max_bytes, free_bytes, expected_error
):
    response = FakeResponse(headers=headers, body=body)
    install_fake_connections(monkeypatch, [response], free_bytes=free_bytes)
    monkeypatch.setattr(media, "public_target", lambda url: ("img.xhscdn.com", "93.184.216.34"))
    target = tmp_path / "rejected.bin"

    with pytest.raises(MediaRejected, match=expected_error):
        media.download_public_media("https://img.xhscdn.com/media", target, max_bytes=max_bytes)

    assert not target.exists()
    assert not target.with_suffix(".part").exists()
    assert response.closed


def test_download_rechecks_disk_quota_while_streaming_and_removes_partial_file(monkeypatch, tmp_path):
    response = FakeResponse(
        headers={"Content-Type": "image/png", "Content-Length": 10},
        chunks=[b"first", b"later"],
    )
    connections = install_fake_connections(monkeypatch, [response])
    free_values = iter([1 << 40, 1 << 40, 0])
    monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(free_values)))
    monkeypatch.setattr(media, "public_target", lambda url: ("img.xhscdn.com", "93.184.216.34"))
    target = tmp_path / "partial.png"

    with pytest.raises(MediaRejected, match="MEDIA_QUOTA_EXCEEDED"):
        media.download_public_media("https://img.xhscdn.com/media", target)

    assert response.read_calls == 2
    assert not target.exists()
    assert not target.with_suffix(".part").exists()
    assert response.closed and connections[0].closed


def test_download_cancellation_removes_partial_media_file(monkeypatch, tmp_path):
    content = png_bytes()
    response = FakeResponse(
        headers={"Content-Type": "image/png", "Content-Length": len(content)},
        chunks=[content[:12], content[12:]],
    )
    connections = install_fake_connections(monkeypatch, [response])
    monkeypatch.setattr(media, "public_target", lambda url: ("img.xhscdn.com", "93.184.216.34"))
    target = tmp_path / "cancelled.png"
    cancelled_calls = []

    with pytest.raises(MediaRejected, match="MEDIA_CANCELLED"):
        media.download_public_media(
            "https://img.xhscdn.com/media", target,
            cancelled=lambda: cancelled_calls.append(True) or True,
        )

    assert cancelled_calls == [True]
    assert response.read_calls == 1
    assert not target.exists() and not target.with_suffix(".part").exists()
    assert response.closed and connections[0].closed


@pytest.mark.parametrize("location", [
    "https://www.xiaohongshu.com.attacker.test/steal",
    "http://127.0.0.1/private",
    "https://img.xhscdn.com:8443/private",
    "https://user:password@img.xhscdn.com/private",
])
def test_download_revalidates_redirects_before_opening_a_second_connection(monkeypatch, tmp_path, location):
    first = FakeResponse(status=302, headers={"Location": location})
    connections = install_fake_connections(monkeypatch, [first])
    monkeypatch.setattr(
        media.socket, "getaddrinfo", lambda *args, **kwargs: address_records(["93.184.216.34"])
    )
    target = tmp_path / "redirected.bin"

    with pytest.raises(MediaRejected, match="MEDIA_ADDRESS_REJECTED"):
        media.download_public_media("https://img.xhscdn.com/start", target)

    assert len(connections) == 1
    assert first.closed and connections[0].closed
    assert not target.exists()
    assert not target.with_suffix(".part").exists()


def test_download_stops_after_redirect_limit(monkeypatch, tmp_path):
    responses = [
        FakeResponse(status=302, headers={"Location": f"/redirect/{index + 1}"})
        for index in range(5)
    ]
    connections = install_fake_connections(monkeypatch, responses)
    monkeypatch.setattr(media, "public_target", lambda url: ("img.xhscdn.com", "93.184.216.34"))
    target = tmp_path / "redirect-limit.bin"

    with pytest.raises(MediaRejected, match="MEDIA_REDIRECT_LIMIT"):
        media.download_public_media("https://img.xhscdn.com/start", target)

    assert len(connections) == 5
    assert all(response.closed for response in responses)
    assert all(connection.closed for connection in connections)
    assert not target.exists()


def test_download_decodes_mp4_before_archiving(monkeypatch, tmp_path):
    content = b"\x00\x00\x00\x18ftypisom" + b"encoded-video-data"
    response = FakeResponse(headers={"Content-Type": "video/mp4", "Content-Length": len(content)}, body=content)
    connections = install_fake_connections(monkeypatch, [response])
    monkeypatch.setattr(media, "public_target", lambda url: ("video.xhscdn.com", "93.184.216.34"))
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", SimpleNamespace(get_ffmpeg_exe=lambda: "/test/ffmpeg"))
    calls = []

    def fake_run(command, *, capture_output, timeout):
        calls.append((command, capture_output, timeout))
        return SimpleNamespace(
            returncode=0, stdout=b"",
            stderr=b"Duration: 00:00:03.50, start: 0.000000, bitrate: 500 kb/s\nStream #0:0: Video: h264, yuv420p, 640x360",
        )

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    target = tmp_path / "video.mp4"

    result = media.download_public_media("https://video.xhscdn.com/video.mp4", target)

    assert {key: result[key] for key in ("sha256", "size", "mime", "width", "height", "variant")} == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mime": "video/mp4",
        "width": 640,
        "height": 360,
        "variant": "visible_web_media",
    }
    assert result["duration"] == 3.5
    assert target.read_bytes() == content
    assert calls == [([
        "/test/ffmpeg", "-hide_banner", "-i", str(target.with_suffix(".part")),
        "-frames:v", "1", "-f", "null", "-",
    ], True, 60)]
    assert connections[0].closed and response.closed


def test_download_rejects_mp4_that_cannot_be_decoded_and_cleans_partial_file(monkeypatch, tmp_path):
    content = b"\x00\x00\x00\x18ftypisom" + b"broken-video-data"
    response = FakeResponse(headers={"Content-Type": "video/mp4", "Content-Length": len(content)}, body=content)
    connections = install_fake_connections(monkeypatch, [response])
    monkeypatch.setattr(media, "public_target", lambda url: ("video.xhscdn.com", "93.184.216.34"))
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", SimpleNamespace(get_ffmpeg_exe=lambda: "/test/ffmpeg"))
    monkeypatch.setattr(
        media.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=b"decode failed")
    )
    target = tmp_path / "invalid.mp4"

    with pytest.raises(MediaRejected, match="MEDIA_DECODE_REJECTED"):
        media.download_public_media("https://video.xhscdn.com/invalid.mp4", target)

    assert not target.exists()
    assert not target.with_suffix(".part").exists()
    assert connections[0].closed and response.closed
