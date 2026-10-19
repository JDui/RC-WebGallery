from __future__ import annotations

from datetime import datetime
import re
import json
from urllib.parse import urlsplit

from app.services.utils import TIMEZONE, now_iso
from app.services.xhs.models import identifier


def media_url(value):
    if not isinstance(value, str):
        return None
    if value.startswith('//'):
        value = 'https:' + value
    elif value.startswith('http://'):
        value = 'https://' + value[7:]
    return value if value.startswith('https://') else None


def extract_detail_note(state, note_id):
    if not isinstance(state, dict):
        return None
    note_state = state.get('note') or {}
    entries = note_state.get('noteDetailMap') or note_state.get('note_detail_map') or {}
    if not isinstance(entries, dict):
        return None
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        card = entry.get('note') or entry.get('note_card') or entry.get('noteCard')
        if isinstance(card, dict) and str(card.get('noteId') or card.get('note_id') or card.get('id')) == note_id:
            return card
    return None


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


def origin_video_url(value):
    url = media_url(value)
    if url:
        return url
    if (not isinstance(value, str) or len(value) > 2048 or value.startswith('/')
            or not re.fullmatch(r'[A-Za-z0-9_./-]+', value)
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        return None
    return 'https://sns-video-bd.xhscdn.com/' + value


def extract_video_url(video):
    if not isinstance(video, dict):
        return None
    variants = [video]
    media_v2 = video.get('mediaV2') or video.get('media_v2')
    if isinstance(media_v2, str) and len(media_v2) <= 256 * 1024:
        try:
            media_v2 = json.loads(media_v2)
        except (ValueError, RecursionError):
            media_v2 = None
    if isinstance(media_v2, dict):
        variants.append(media_v2)
    for variant in variants:
        media = variant.get('media') or {}
        streams = media.get('stream') if isinstance(media, dict) else None
        streams = streams or variant.get('stream') or {}
        if not isinstance(streams, dict):
            continue
        groups = ['h264', 'h265', 'av1', *(key for key in streams if key not in {'h264', 'h265', 'av1'})]
        entries = [entry for key in groups if isinstance(streams.get(key), list)
                   for entry in streams[key] if isinstance(entry, dict)]
        def codec_priority(entry):
            codec = str(entry.get('video_codec') or entry.get('videoCodec') or '').lower()
            if '264' in codec or 'avc' in codec:
                return 0
            if 'av1' in codec:
                return 1
            return 3 if '265' in codec or 'hevc' in codec else 2
        for entry in sorted(entries, key=codec_priority):
            backups = entry.get('backup_urls') or entry.get('backupUrls') or []
            if not isinstance(backups, list):
                backups = []
            for candidate in [entry.get('master_url'), entry.get('masterUrl'), *backups]:
                url = media_url(candidate)
                if url:
                    return url
    for variant in variants:
        consumer = variant.get('consumer') or {}
        if isinstance(consumer, dict):
            url = origin_video_url(consumer.get('origin_video_key') or consumer.get('originVideoKey'))
            if url:
                return url
    return None


def video_diagnostic(video):
    fields = []
    def visit(value, path, depth):
        if len(fields) >= 80 or depth > 6:
            return
        if isinstance(value, dict):
            fields.append(path + ':dict[' + str(min(len(value), 100)) + ']')
            for key, child in value.items():
                if isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,47}', key):
                    visit(child, path + '.' + key, depth + 1)
        elif isinstance(value, list):
            fields.append(path + ':list[' + str(min(len(value), 100)) + ']')
            if value:
                visit(value[0], path + '[]', depth + 1)
        else:
            fields.append(path + ':' + ('null' if value is None else type(value).__name__))
            if isinstance(value, str) and path.endswith(('.mediaV2', '.media_v2')) and len(value) <= 256 * 1024:
                try:
                    decoded = json.loads(value)
                except (ValueError, RecursionError):
                    return
                visit(decoded, path + '.decoded', depth + 1)
    visit(video, 'video', 0)
    return fields[:80]


def normalize_note(raw: dict, *, detail: bool = False) -> tuple[dict, list[tuple[int, str, str]]]:
    card = raw.get('note_card') or raw.get('noteCard') or raw
    note_id = identifier(raw.get('id') or raw.get('note_id') or card.get('note_id') or card.get('noteId') or card.get('id'))
    user = card.get('user') or {}
    author = user.get('user_id') or user.get('userId') or ''
    published = card.get('time') or card.get('publish_time') or card.get('publishTime')
    try:
        if isinstance(published, (int, float)) and not isinstance(published, bool):
            published = datetime.fromtimestamp(published / 1000 if published > 100000000000 else published, TIMEZONE)
        elif isinstance(published, str):
            published = datetime.fromisoformat(published.replace('Z', '+00:00'))
        else:
            published = None
        if published and published.year < 2010:
            published = None
        published = published.replace(tzinfo=TIMEZONE).isoformat() if published and published.tzinfo is None else published.isoformat() if published else None
    except (ValueError, OverflowError, OSError):
        published = None
    assets = []
    candidates = []
    images = card.get('image_list') or card.get('imageList') or []
    if detail and not images and (card.get('type') == 'video' or card.get('video')) and isinstance(card.get('cover'), dict):
        images = [card['cover']]
    for ordinal, image in enumerate(images[:100]):
        options = image.get('info_list') or image.get('infoList') or []
        url = image.get('url_default') or image.get('urlDefault') or image.get('url') or image.get('url_pre') or image.get('urlPre')
        for option in sorted(options, key=lambda item: str(item.get('image_scene') or item.get('imageScene') or '').upper() != 'WB_DFT'):
            if str(option.get('image_scene') or option.get('imageScene') or '').upper() in {'WB_DFT', 'CRD_WM_WEBP'}:
                url = option.get('url') or url
                break
        url = media_url(url)
        if url:
            assets.append({'ordinal': ordinal, 'kind': 'image'})
            candidates.append((ordinal, 'image', url))
        live = image.get('live_photo') or image.get('livePhoto') or image.get('video') or {}
        if isinstance(live, dict):
            motion_url = media_url(live.get('video_url') or live.get('videoUrl') or live.get('url'))
            if motion_url:
                assets.append({'ordinal': ordinal, 'kind': 'livephoto'})
                candidates.append((ordinal, 'livephoto', motion_url))
    video = card.get('video') or {}
    video_url = extract_video_url(video)
    if video_url:
        assets.append({'ordinal': 0, 'kind': 'video'})
        candidates.append((0, 'video', video_url))
    title = re.sub(r'[\x00-\x1f]', ' ', str(card.get('display_title') or card.get('title') or note_id))[:1000]
    note = {'note_id': note_id, 'url': f'https://www.xiaohongshu.com/explore/{note_id}', 'title': title,
            'author_id': author, 'type': 'video' if card.get('type') == 'video' or bool(video) else 'image',
            'published_at': published, 'observed_at': now_iso(), 'assets': assets}
    return note, candidates
