import asyncio
from collections import deque
import json

import pytest

from xhs_worker.runtime import BrowserFault, BrowserRuntime


class FakePage:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, payload, *, status=200, url="https://www.xiaohongshu.com/note/like/page"):
        self.status = status
        self.url = url
        self.payload = payload

    async def all_headers(self):
        return {"content-length": str(len(json.dumps(self.payload).encode()))}

    async def body(self):
        return json.dumps(self.payload).encode()


def test_runtime_cancel_marks_session_and_closes_active_page(tmp_path):
    runtime = BrowserRuntime(tmp_path / "private", "synthetic-worker-token-0123456789")
    page = FakePage()
    runtime.session_id = "session_synthetic_001"
    runtime.page = page
    runtime.state = "RUNNING"

    result = asyncio.run(runtime.cancel(runtime.session_id))

    assert result == {"state": "CANCELLED"}
    assert runtime.cancel_event.is_set()
    assert runtime.state == "CANCELLED"
    assert page.closed is True
    with pytest.raises(BrowserFault, match="SESSION_EXPIRED"):
        asyncio.run(runtime.cancel("another-session"))


def test_capture_rejects_repeated_cursor_and_discards_unreliable_queue(tmp_path):
    runtime = BrowserRuntime(tmp_path / "private", "synthetic-worker-token-0123456789")
    payload = {"data": {"notes": [{"id": "synthetic_note"}], "has_more": True, "cursor": "cursor-1"}}

    asyncio.run(runtime._capture(FakeResponse(payload)))
    assert len(runtime.queue) == 1
    asyncio.run(runtime._capture(FakeResponse(payload)))

    assert runtime.state == "CURSOR_ERROR"
    assert not runtime.queue


def test_capture_rejects_page_when_cursor_queue_would_overflow(tmp_path):
    runtime = BrowserRuntime(tmp_path / "private", "synthetic-worker-token-0123456789")
    runtime.queue = deque([([], False, True)], maxlen=1)
    payload = {"data": {"notes": [{"id": "synthetic_note"}], "has_more": True, "cursor": "cursor-2"}}

    asyncio.run(runtime._capture(FakeResponse(payload)))

    assert runtime.state == "CURSOR_ERROR"
    assert not runtime.queue


def test_next_page_does_not_return_cached_page_after_worker_error(tmp_path):
    runtime = BrowserRuntime(tmp_path / "private", "synthetic-worker-token-0123456789")
    runtime.session_id = "session_synthetic_001"
    runtime.state = "NEEDS_REAUTH"
    cached = ([{"id": "stale_note"}], True, True)
    runtime.queue.append(cached)

    with pytest.raises(BrowserFault, match="NEEDS_REAUTH"):
        asyncio.run(runtime.next_page(runtime.session_id))

    assert list(runtime.queue) == [cached]
