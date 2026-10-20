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
    value = value.replace("\\u002F", "/").replace("\\/", "/")
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
    if isinstance(value, str) and value.startswith('/') and not value.startswith('//'):
        value = value.lstrip('/')
    if (not isinstance(value, str) or len(value) > 2048 or value.startswith('/')
            or not re.fullmatch(r'[A-Za-z0-9_./-]+', value)
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        return None
    return 'https://sns-video-bd.xhscdn.com/' + value


def extract_video_urls(video):
    """Return video URL candidates in best-effort unwatermarked-first order."""
    if not isinstance(video, dict):
        return []
    variants = [video]
    media_v2 = video.get('mediaV2') or video.get('media_v2')
    if isinstance(media_v2, str) and len(media_v2) <= 256 * 1024:
        try:
            media_v2 = json.loads(media_v2)
        except (ValueError, RecursionError):
            media_v2 = None
    if isinstance(media_v2, dict):
        variants.append(media_v2)
    urls = []
    def add_url(value):
        url = media_url(value)
        if url and url not in urls and len(urls) < 32:
            urls.append(url)
    # The original upload key is the strongest known candidate for a clean source.
    for variant in variants:
        consumers = [variant.get('consumer')]
        nested_video = variant.get('video')
        if isinstance(nested_video, dict):
            consumers.append(nested_video.get('consumer'))
        for consumer in consumers:
            if not isinstance(consumer, dict):
                continue
            key = consumer.get('origin_video_key') or consumer.get('originVideoKey')
            origin = origin_video_url(key)
            if origin:
                add_url(origin)
        origin = origin_video_url(variant.get('origin_video_key') or variant.get('originVideoKey'))
        if origin:
            add_url(origin)
    # Some responses expose an unwatermarked screencast URL only inside mediaV2.opaque1.
    for variant in variants:
        nested_video = variant.get('video') if isinstance(variant.get('video'), dict) else {}
        opaque1 = nested_video.get('opaque1') or variant.get('opaque1') or {}
        if isinstance(opaque1, dict):
            for key in ('hd_screencast_stream', 'default_screencast_stream'):
                value = opaque1.get(key)
                if isinstance(value, dict):
                    for url_key in ('masterUrl', 'master_url', 'url', 'streamUrl', 'stream_url'):
                        add_url(value.get(url_key))
                elif isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, dict):
                            for url_key in ('masterUrl', 'master_url', 'url', 'streamUrl', 'stream_url'):
                                add_url(entry.get(url_key))
                        else:
                            add_url(entry)
                else:
                    add_url(value)
    stream_candidates = []
    ordinal = 0
    for variant in variants:
        media = variant.get('media') or {}
        streams = media.get('stream') if isinstance(media, dict) else None
        streams = streams or variant.get('stream') or {}
        if not isinstance(streams, dict):
            continue
        groups = ['h264', 'h265', 'av1', *(key for key in streams if key not in {'h264', 'h265', 'av1'})]
        for codec in groups:
            entries = streams.get(codec)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                stream_type = str(entry.get('stream_type') or entry.get('streamType') or '').strip()
                stream_desc = str(entry.get('stream_desc') or entry.get('streamDesc') or '').upper()
                watermark_hint = bool(re.search(r'\bWM\b|WATER.?MARK|水印', stream_desc, re.IGNORECASE))
                if stream_type in {'259', '309'} or watermark_hint:
                    watermark_rank = 100
                elif stream_type in {'258', '301'} or 'X264_MP4' in stream_desc:
                    watermark_rank = 0
                else:
                    watermark_rank = 10
                codec_value = str(entry.get('video_codec') or entry.get('videoCodec') or codec).lower()
                codec_rank = 0 if ('264' in codec_value or 'avc' in codec_value or codec == 'h264') else (
                    1 if ('av1' in codec_value or codec == 'av1') else 2
                )
                try:
                    height = int(entry.get('height') or entry.get('video_height') or 0)
                except (TypeError, ValueError):
                    height = 0
                try:
                    bitrate = int(entry.get('video_bitrate') or entry.get('videoBitrate') or entry.get('bitrate') or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                backups = entry.get('backup_urls') or entry.get('backupUrls') or []
                if not isinstance(backups, list):
                    backups = []
                for candidate in [entry.get('master_url'), entry.get('masterUrl'), *backups]:
                    url = media_url(candidate)
                    if url and url not in urls:
                        stream_candidates.append((watermark_rank, -height, -bitrate, codec_rank, ordinal, url))
                        ordinal += 1
    for _, _, _, _, _, url in sorted(stream_candidates):
        add_url(url)
    return urls[:16]


def extract_video_url(video):
    candidates = extract_video_urls(video)
    return candidates[0] if candidates else None
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
    title_value = card.get('display_title') or card.get('title') or ''
    if not isinstance(title_value, str) or title_value.strip() == note_id:
        title_value = ''
    if not title_value.strip():
        title_value = card.get('desc') or card.get('content') or card.get('description') or card.get('note_desc') or ''
    title = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f]', ' ', str(title_value))).strip()[:1000] or note_id
    note = {'note_id': note_id, 'url': f'https://www.xiaohongshu.com/explore/{note_id}', 'title': title,
            'author_id': author, 'type': 'video' if card.get('type') == 'video' or bool(video) else 'image',
            'published_at': published, 'observed_at': now_iso(), 'assets': assets}
    return note, candidates
