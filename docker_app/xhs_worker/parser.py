from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit

from app.services.utils import TIMEZONE, now_iso
from app.services.xhs.models import identifier


def response_page(payload: dict) -> tuple[list[dict], bool, bool] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if not isinstance(data, dict):
        return None
    notes = data.get('notes', data.get('items'))
    if not isinstance(notes, list):
        return None
    has_more = data.get('has_more', data.get('hasMore'))
    return notes[:100], has_more is False and len(notes) <= 100, type(has_more) is bool and len(notes) <= 100


def normalize_note(raw: dict) -> tuple[dict, list[tuple[int, str, str]]]:
    card = raw.get('note_card') or raw.get('noteCard') or raw
    note_id = identifier(raw.get('id') or raw.get('note_id') or card.get('note_id') or card.get('noteId') or card.get('id'))
    user = card.get('user') or {}
    author = user.get('user_id') or user.get('userId') or ''
    published = card.get('time') or card.get('publish_time')
    if isinstance(published, (int, float)):
        published = datetime.fromtimestamp(published / 1000 if published > 100000000000 else published, TIMEZONE).isoformat()
    else:
        published = None
    assets = []
    candidates = []
    images = card.get('image_list') or card.get('imageList') or []
    if not images and isinstance(card.get('cover'), dict):
        # A list cover is not evidence that all note media has been enumerated.
        images = []
    for ordinal, image in enumerate(images[:100]):
        options = image.get('info_list') or image.get('infoList') or []
        url = image.get('url_default') or image.get('urlDefault') or image.get('url')
        for option in sorted(options, key=lambda item: str(item.get('image_scene') or item.get('imageScene') or '').upper() != 'WB_DFT'):
            if str(option.get('image_scene') or option.get('imageScene') or '').upper() in {'WB_DFT', 'CRD_WM_WEBP'}:
                url = option.get('url') or url
                break
        if isinstance(url, str) and url.startswith('https://'):
            assets.append({'ordinal': ordinal, 'kind': 'image'})
            candidates.append((ordinal, 'image', url))
        live = image.get('live_photo') or image.get('livePhoto') or image.get('video') or {}
        if isinstance(live, dict):
            motion_url = live.get('video_url') or live.get('videoUrl') or live.get('url')
            if isinstance(motion_url, str) and motion_url.startswith('https://'):
                assets.append({'ordinal': ordinal, 'kind': 'livephoto'})
                candidates.append((ordinal, 'livephoto', motion_url))
    video = card.get('video') or {}
    media = video.get('media') or {}
    streams = media.get('stream') or video.get('stream') or {}
    for codec in ('h264', 'h265', 'av1'):
        entries = streams.get(codec) or []
        if isinstance(entries, list) and entries:
            url = entries[0].get('master_url') or entries[0].get('masterUrl')
            if isinstance(url, str) and url.startswith('https://'):
                assets.append({'ordinal': 0, 'kind': 'video'})
                candidates.append((0, 'video', url))
                break
    title = re.sub(r'[\x00-\x1f]', ' ', str(card.get('display_title') or card.get('title') or note_id))[:1000]
    note = {'note_id': note_id, 'url': f'https://www.xiaohongshu.com/explore/{note_id}', 'title': title,
            'author_id': author, 'type': 'video' if card.get('type') == 'video' else 'image',
            'published_at': published, 'observed_at': now_iso(), 'assets': assets}
    return note, candidates
