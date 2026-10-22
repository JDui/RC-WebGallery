#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

API_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
API_DETAIL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
FEATURES = (
    "itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,"
    "decorationCard,onlyfansAssetsV2,ugcDelete,onlyfansQaCard,commentsNewVersion"
)
TIMEZONE = ZoneInfo("Asia/Shanghai")
STATE_BEGIN = "<!-- DOWNLOAD_STATE_BEGIN -->"
STATE_END = "<!-- DOWNLOAD_STATE_END -->"


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    return name


def derive_chrome_key(password: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1003,
    )
    return kdf.derive(password)


def decrypt_chrome_cookie(host_key: str, encrypted_value: bytes, key: bytes) -> str:
    payload = encrypted_value[3:] if encrypted_value.startswith(b"v10") else encrypted_value
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(payload) + decryptor.finalize()
    plaintext = plaintext[: -plaintext[-1]]

    host_hash = hashlib.sha256(host_key.encode("utf-8")).digest()
    if plaintext.startswith(host_hash):
        plaintext = plaintext[len(host_hash):]
    return plaintext.decode("utf-8")


def load_cookie_from_env() -> Optional[str]:
    raw = os.environ.get("BILI_COOKIE", "").strip()
    if raw:
        return raw

    sessdata = os.environ.get("BILI_SESSDATA", "").strip()
    if not sessdata:
        return None

    parts = [f"SESSDATA={sessdata}"]
    bili_jct = os.environ.get("BILI_BILI_JCT", "").strip()
    dede_user_id = os.environ.get("BILI_DEDEUSERID", "").strip()
    if bili_jct:
        parts.append(f"bili_jct={bili_jct}")
    if dede_user_id:
        parts.append(f"DedeUserID={dede_user_id}")
    return "; ".join(parts)


def chrome_cookie_db(profile: str) -> pathlib.Path:
    return pathlib.Path.home() / "Library/Application Support/Google/Chrome" / profile / "Cookies"


def chrome_safe_storage_password() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-w", "-a", "Chrome", "-s", "Chrome Safe Storage"],
        text=True,
    ).strip()


def load_cookie_from_chrome(profile: str = "Default") -> str:
    db_path = chrome_cookie_db(profile)
    if not db_path.exists():
        raise FileNotFoundError(f"Chrome cookie DB not found: {db_path}")

    password = chrome_safe_storage_password().encode("utf-8")
    key = derive_chrome_key(password)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        """
        select host_key, name, encrypted_value
        from cookies
        where host_key in ('.bilibili.com', '.bilibili.cn')
          and name in ('SESSDATA', 'bili_jct', 'DedeUserID')
        order by host_key = '.bilibili.com' desc, name
        """
    )

    values: Dict[str, str] = {}
    for host_key, name, encrypted_value in cur.fetchall():
        if name in values:
            continue
        try:
            values[name] = decrypt_chrome_cookie(host_key, encrypted_value, key)
        except Exception:
            continue

    conn.close()

    missing = [name for name in ("SESSDATA", "bili_jct", "DedeUserID") if not values.get(name)]
    if missing:
        raise RuntimeError(f"Missing Chrome cookies: {', '.join(missing)}")

    return "; ".join(
        [
            f"SESSDATA={values['SESSDATA']}",
            f"bili_jct={values['bili_jct']}",
            f"DedeUserID={values['DedeUserID']}",
        ]
    )


def resolve_cookie(profile: str) -> str:
    env_cookie = load_cookie_from_env()
    if env_cookie:
        print("[AUTH] use cookie from env")
        return env_cookie

    print(f"[AUTH] load cookie from Chrome profile={profile}")
    return load_cookie_from_chrome(profile=profile)


def build_headers(host_mid: int, cookie: str) -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://space.bilibili.com",
        "Referer": f"https://space.bilibili.com/{host_mid}/dynamic",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Cookie": cookie,
    }


def feed_params(host_mid: int, offset: str) -> Dict[str, str]:
    return {
        "host_mid": str(host_mid),
        "offset": offset,
        "timezone_offset": "-480",
        "platform": "web",
        "features": FEATURES,
        "web_location": "333.1387",
    }


def detail_params(dynamic_id: str) -> Dict[str, str]:
    return {
        "id": dynamic_id,
        "timezone_offset": "-480",
        "platform": "web",
        "gaia_source": "main_web",
        "features": FEATURES,
        "web_location": "333.1368",
    }


def request_json(url: str, headers: Dict[str, str], params: Dict[str, str]) -> dict:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"API error: {payload.get('code')} {payload.get('message')}")
    return payload.get("data") or {}


def iter_space_pages(host_mid: int, headers: Dict[str, str]) -> Iterable[Tuple[int, List[dict]]]:
    offset = ""
    page = 0
    seen_offsets = set()

    while True:
        page += 1
        print(f"[PAGE] {page} offset={offset or '<first>'}")
        data = request_json(API_FEED, headers=headers, params=feed_params(host_mid, offset))
        items = data.get("items") or []
        next_offset = data.get("offset") or ""
        has_more = bool(data.get("has_more"))
        print(f"[PAGE] items={len(items)} has_more={has_more} next_offset={next_offset or '<empty>'}")
        yield page, items

        if not has_more or not next_offset or next_offset in seen_offsets:
            print(f"[BREAK] has_more={has_more} next_offset={next_offset or '<empty>'}")
            break

        seen_offsets.add(next_offset)
        offset = next_offset


def fetch_dynamic_detail(dynamic_id: str, headers: Dict[str, str]) -> dict:
    data = request_json(API_DETAIL, headers=headers, params=detail_params(dynamic_id))
    return data.get("item") or {}


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def short_title(text: str) -> str:
    clean = compact_text(text)
    chars: List[str] = []
    for ch in clean:
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            chars.append(ch)
        elif ch in {" ", "-", "_"}:
            chars.append("_")
        if len(chars) >= 5:
            break

    title = "".join(chars).strip("_")
    title = re.sub(r"_+", "_", title)
    return title or "无文案"


def format_pub_time(pub_ts: int) -> str:
    if pub_ts <= 0:
        return "1970-01-01 00:00:00"
    return datetime.fromtimestamp(pub_ts, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def build_folder_base(pub_ts: int, text: str) -> str:
    dt = datetime.fromtimestamp(pub_ts if pub_ts > 0 else 0, TIMEZONE)
    return safe_filename(f"{dt.strftime('%Y%m%d_%H%M%S')}_{short_title(text)}")


def record_key(top_dynamic_id: str, source_dynamic_id: str) -> str:
    return f"{top_dynamic_id}::{source_dynamic_id}"


def load_md_records(md_path: pathlib.Path) -> Dict[str, dict]:
    if not md_path.exists():
        return {}

    text = md_path.read_text(encoding="utf-8")
    start = text.find(STATE_BEGIN)
    end = text.find(STATE_END)
    if start == -1 or end == -1 or end <= start:
        return {}

    payload = text[start + len(STATE_BEGIN) : end].strip()
    if not payload:
        return {}

    try:
        items = json.loads(payload)
    except json.JSONDecodeError:
        return {}

    records = {}
    for item in items:
        key = record_key(str(item["top_dynamic_id"]), str(item["source_dynamic_id"]))
        records[key] = item
    return records


def count_files(folder: pathlib.Path) -> int:
    if not folder.exists():
        return 0
    return sum(1 for path in folder.iterdir() if path.is_file())


def refresh_record_status(record: dict, out_root: pathlib.Path) -> None:
    folder = out_root / record["folder_name"]
    file_count = count_files(folder)
    record["file_count"] = file_count
    record["status"] = "complete" if file_count >= int(record["picture_count"]) else "partial"


def write_downloads_md(md_path: pathlib.Path, out_root: pathlib.Path, records: Dict[str, dict]) -> None:
    ordered = sorted(
        records.values(),
        key=lambda item: (int(item.get("pub_ts", 0)), item["top_dynamic_id"], item["source_dynamic_id"]),
        reverse=True,
    )
    for record in ordered:
        refresh_record_status(record, out_root)

    total_files = sum(int(record.get("file_count", 0)) for record in ordered)
    now_text = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# 粽子淞九图动态下载记录",
        "",
        f"- 更新时间：{now_text}",
        f"- 已记录动态：{len(ordered)}",
        f"- 已保存文件：{total_files}",
        "",
        "| 发布时间 | 前5字 | 图片数 | 已有文件 | 状态 | 顶层动态ID | 源动态ID | 目录 |",
        "| --- | --- | ---: | ---: | --- | --- | --- | --- |",
    ]

    for record in ordered:
        lines.append(
            "| {pub_time} | {text_prefix} | {picture_count} | {file_count} | {status} | "
            "{top_dynamic_id} | {source_dynamic_id} | downloaded/{folder_name} |".format(
                pub_time=record["pub_time"],
                text_prefix=record["text_prefix"].replace("|", "/"),
                picture_count=record["picture_count"],
                file_count=record.get("file_count", 0),
                status=record.get("status", "partial"),
                top_dynamic_id=record["top_dynamic_id"],
                source_dynamic_id=record["source_dynamic_id"],
                folder_name=record["folder_name"],
            )
        )

    lines.extend(
        [
            "",
            STATE_BEGIN,
            json.dumps(ordered, ensure_ascii=False, indent=2),
            STATE_END,
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")


def load_processed_urls(urls_txt: pathlib.Path) -> Set[str]:
    if not urls_txt.exists():
        return set()
    return {line.strip() for line in urls_txt.read_text(encoding="utf-8").splitlines() if line.strip()}


def folder_pub_ts(folder_name: str) -> int:
    match = re.match(r"^(\d{8})_(\d{6})_", folder_name)
    if not match:
        return 0

    date_part, time_part = match.groups()
    try:
        dt = datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")
    except ValueError:
        return 0
    return int(dt.replace(tzinfo=TIMEZONE).timestamp())


def latest_local_pub_ts(records: Dict[str, dict], out_root: pathlib.Path) -> int:
    latest = 0
    for record in records.values():
        try:
            latest = max(latest, int(record.get("pub_ts") or 0))
        except (TypeError, ValueError):
            continue

    for path in out_root.iterdir():
        if path.is_dir():
            latest = max(latest, folder_pub_ts(path.name))
    return latest


def extract_primary_text(item: dict) -> str:
    module_dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
    major = module_dynamic.get("major") or {}
    opus = major.get("opus") or {}
    summary = opus.get("summary") or {}
    text = summary.get("text") or (module_dynamic.get("desc") or {}).get("text") or ""
    return compact_text(text)


def extract_pub_ts(item: dict) -> int:
    author = (item.get("modules") or {}).get("module_author") or {}
    try:
        return int(author.get("pub_ts") or 0)
    except (TypeError, ValueError):
        return 0


def is_top_item(item: dict) -> bool:
    author = (item.get("modules") or {}).get("module_author") or {}
    return bool(author.get("is_top"))


def extract_picture_nodes(item: dict) -> List[dict]:
    module_dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
    major = module_dynamic.get("major") or {}
    draw = major.get("draw") or {}
    if isinstance(draw.get("items"), list) and draw.get("items"):
        return draw["items"]

    opus = major.get("opus") or {}
    if isinstance(opus.get("pics"), list) and opus.get("pics"):
        return opus["pics"]

    return []


def find_nine_pic_blocks(item: dict, include_forwarded: bool) -> List[Tuple[dict, dict, List[dict]]]:
    blocks: List[Tuple[dict, dict, List[dict]]] = []
    pictures = extract_picture_nodes(item)
    if pictures:
        blocks.append((item, item, pictures))

    if include_forwarded:
        orig = item.get("orig")
        if isinstance(orig, dict):
            orig_pictures = extract_picture_nodes(orig)
            if orig_pictures:
                blocks.append((item, orig, orig_pictures))

    return blocks


def make_unique_folder_name(base_name: str, used_names: Set[str]) -> str:
    candidate = base_name
    index = 2
    while candidate in used_names:
        candidate = f"{base_name}_{index}"
        index += 1
    used_names.add(candidate)
    return candidate


def build_record(
    top_item: dict,
    source_item: dict,
    picture_count: int,
    used_folder_names: Set[str],
    existing_record: Optional[dict] = None,
) -> dict:
    top_id = str(top_item.get("id_str") or "unknown")
    source_id = str(source_item.get("id_str") or "unknown")
    pub_ts = extract_pub_ts(top_item) or extract_pub_ts(source_item)
    text = extract_primary_text(top_item) or extract_primary_text(source_item) or "无文案"

    folder_name = existing_record["folder_name"] if existing_record else ""
    if not folder_name:
        folder_name = make_unique_folder_name(build_folder_base(pub_ts, text), used_folder_names)
    else:
        used_folder_names.add(folder_name)

    return {
        "top_dynamic_id": top_id,
        "source_dynamic_id": source_id,
        "pub_ts": pub_ts,
        "pub_time": format_pub_time(pub_ts),
        "text_prefix": short_title(text),
        "picture_count": picture_count,
        "folder_name": folder_name,
        "file_count": int(existing_record.get("file_count", 0)) if existing_record else 0,
        "status": existing_record.get("status", "partial") if existing_record else "partial",
    }


def image_url(pic: dict) -> str:
    url = (pic.get("src") or pic.get("img_src") or pic.get("url") or "").strip()
    return url.replace("http://", "https://")


def download_file(url: str, out_path: pathlib.Path, headers: Dict[str, str]) -> None:
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    out_path.write_bytes(resp.content)


def migrate_legacy_layout(
    out_root: pathlib.Path,
    headers: Dict[str, str],
    records: Dict[str, dict],
    used_folder_names: Set[str],
) -> bool:
    legacy_manifest = pathlib.Path("matches_9pics.jsonl")
    if not legacy_manifest.exists():
        return False

    print("[MIGRATE] found legacy manifest, migrating old folders")
    migrated = False
    for line in legacy_manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue

        top_id = str(item.get("top_dynamic_id") or "")
        source_id = str(item.get("source_dynamic_id") or "")
        picture_count = int(item.get("picture_count") or 9)
        if not top_id or not source_id:
            continue

        key = record_key(top_id, source_id)
        if key in records:
            continue

        top_item = fetch_dynamic_detail(top_id, headers)
        source_item = top_item if source_id == top_id else fetch_dynamic_detail(source_id, headers)
        record = build_record(top_item, source_item, picture_count, used_folder_names)

        legacy_folder = pathlib.Path(str(item.get("folder") or ""))
        if not legacy_folder.exists():
            fallback_name = top_id if top_id == source_id else f"{top_id}__orig_{source_id}"
            legacy_folder = out_root / fallback_name

        target_folder = out_root / record["folder_name"]
        if legacy_folder.exists() and legacy_folder.resolve() != target_folder.resolve():
            shutil.move(str(legacy_folder), str(target_folder))
            migrated = True
            print(f"[MIGRATE] {legacy_folder.name} -> {record['folder_name']}")

        refresh_record_status(record, out_root)
        records[key] = record

    return migrated


def rebuild_url_index(urls_txt: pathlib.Path, records: Dict[str, dict], headers: Dict[str, str]) -> Set[str]:
    processed_urls = set()
    for record in records.values():
        dynamic_id = record["source_dynamic_id"]
        item = fetch_dynamic_detail(dynamic_id, headers)
        for pic in extract_picture_nodes(item):
            url = image_url(pic)
            if url:
                processed_urls.add(url)

    urls_txt.write_text(
        "\n".join(sorted(processed_urls)) + ("\n" if processed_urls else ""),
        encoding="utf-8",
    )
    return processed_urls


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all images from Bilibili dynamics that contain exactly 9 pictures."
    )
    parser.add_argument("--host-mid", type=int, default=31968078, help="Bilibili UID")
    parser.add_argument("--out-root", default="downloaded", help="Output directory")
    parser.add_argument("--chrome-profile", default="Default", help="Chrome profile name")
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between image downloads")
    parser.add_argument(
        "--skip-forwarded",
        action="store_true",
        help="Ignore forwarded dynamics whose original content contains 9 pictures",
    )
    args = parser.parse_args()

    cookie = resolve_cookie(profile=args.chrome_profile)
    headers = build_headers(host_mid=args.host_mid, cookie=cookie)

    out_root = pathlib.Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    downloads_md = pathlib.Path("downloads.md")
    urls_txt = pathlib.Path("urls_9pics.txt")

    records = load_md_records(downloads_md)
    used_folder_names = {record["folder_name"] for record in records.values() if record.get("folder_name")}
    used_folder_names.update(path.name for path in out_root.iterdir() if path.is_dir())

    migrated = migrate_legacy_layout(out_root, headers, records, used_folder_names)
    processed_urls = rebuild_url_index(urls_txt, records, headers) if migrated else load_processed_urls(urls_txt)
    write_downloads_md(downloads_md, out_root, records)

    legacy_manifest = pathlib.Path("matches_9pics.jsonl")
    if records and legacy_manifest.exists():
        legacy_manifest.unlink()

    has_existing_downloads = any(path.is_dir() for path in out_root.iterdir())
    incremental_cutoff_ts = latest_local_pub_ts(records, out_root) if has_existing_downloads else 0
    if incremental_cutoff_ts > 0:
        print(f"[INCREMENTAL] cutoff={format_pub_time(incremental_cutoff_ts)}")

    matched_dynamics = 0
    collected_urls = 0
    downloaded_images = 0
    reached_old_items = False

    for page_num, items in iter_space_pages(args.host_mid, headers=headers):
        page_has_newer_items = False

        for item in items:
            item_pub_ts = extract_pub_ts(item)
            if incremental_cutoff_ts == 0 or item_pub_ts == 0 or item_pub_ts > incremental_cutoff_ts:
                page_has_newer_items = True

            matches = find_nine_pic_blocks(item, include_forwarded=not args.skip_forwarded)
            if not matches:
                continue

            for top_item, source_item, pictures in matches:
                top_pub_ts = extract_pub_ts(top_item) or extract_pub_ts(source_item)
                if incremental_cutoff_ts > 0 and top_pub_ts > 0 and top_pub_ts <= incremental_cutoff_ts:
                    continue

                matched_dynamics += 1
                top_id = str(top_item.get("id_str") or "unknown")
                source_id = str(source_item.get("id_str") or "unknown")
                key = record_key(top_id, source_id)

                record = build_record(
                    top_item,
                    source_item,
                    len(pictures),
                    used_folder_names,
                    existing_record=records.get(key),
                )
                records[key] = record

                dynamic_dir = out_root / record["folder_name"]
                dynamic_dir.mkdir(parents=True, exist_ok=True)
                print(
                    f"[MATCH] top={record['top_dynamic_id']} source={record['source_dynamic_id']} "
                    f"folder={record['folder_name']}"
                )

                for index, pic in enumerate(pictures, start=1):
                    url = image_url(pic)
                    if not url:
                        continue

                    if url not in processed_urls:
                        with urls_txt.open("a", encoding="utf-8") as f:
                            f.write(url + "\n")
                        processed_urls.add(url)
                        collected_urls += 1

                    parsed = urlparse(url)
                    basename = os.path.basename(parsed.path) or f"{record['source_dynamic_id']}_{index:02d}.jpg"
                    out_path = dynamic_dir / safe_filename(basename)
                    if out_path.exists() and out_path.stat().st_size > 0:
                        continue

                    try:
                        download_file(url, out_path, headers=headers)
                        downloaded_images += 1
                        print(f"[SAVE] {record['folder_name']} -> {out_path.name}")
                    except Exception as exc:
                        print(f"[FAIL] {record['folder_name']} {url} err={exc}")
                    time.sleep(args.sleep)

                refresh_record_status(record, out_root)
                write_downloads_md(downloads_md, out_root, records)

        if incremental_cutoff_ts > 0 and not page_has_newer_items and page_num > 1:
            print(f"[STOP] reached cutoff page with no newer items after {format_pub_time(incremental_cutoff_ts)}")
            reached_old_items = True
            break

    write_downloads_md(downloads_md, out_root, records)

    print()
    print(f"Matched dynamics: {matched_dynamics}")
    print(f"New URL collected: {collected_urls}")
    print(f"Images saved this run: {downloaded_images}")
    print(f"Reached cutoff: {reached_old_items}")
    print(f"Output folder: {out_root.resolve()}")
    print(f"URL list: {urls_txt.resolve()}")
    print(f"Record file: {downloads_md.resolve()}")


if __name__ == "__main__":
    main()
