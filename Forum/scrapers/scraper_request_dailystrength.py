
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


should_stop = False


def signal_handler(sig, frame):
    global should_stop
    print("\n[INFO] Stopping requested by user...")
    should_stop = True


signal.signal(signal.SIGINT, signal_handler)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}

AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

DISCUSSION_RE = re.compile(r"/discussion/([^/?#]+)", re.I)
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROFILE_ID_RE = re.compile(r"/user/profile/(\d+)", re.I)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_parent_dir(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, obj: dict):
    ensure_parent_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def clean_text(value: str) -> str:
    value = value or ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_multiline_text(value: str) -> str:
    value = value or ""
    value = value.replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", x).strip() for x in value.splitlines()]
    lines = [x for x in lines if x]
    return "\n".join(lines).strip()


def safe_int(value, default=0):
    if value is None:
        return default
    s = re.sub(r"[^\d]", "", str(value))
    return int(s) if s.isdigit() else default


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def load_existing_thread_ids(path: Path) -> Set[str]:
    existing = set()
    if not path.exists():
        return existing

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                thread_id = str(obj.get("thread_id", "")).strip()
                if thread_id:
                    existing.add(thread_id)
            except Exception:
                continue
    return existing


def build_session(cfg: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    request_cfg = cfg.get("request", {})
    max_retries = int(request_cfg.get("max_retries", 3))
    backoff_factor = float(request_cfg.get("backoff_factor", 1.0))

    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_html(session: requests.Session, url: str, timeout: int, referer: str = "") -> str:
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer

    response = session.get(url, headers=headers, timeout=(10, timeout), allow_redirects=True)
    response.raise_for_status()
    return response.text


def fetch_ajax_listing(session: requests.Session, ajax_url: str, referer: str, timeout: int) -> dict:
    headers = dict(DEFAULT_HEADERS)
    headers.update(AJAX_HEADERS)
    headers["Referer"] = referer

    response = session.get(ajax_url, headers=headers, timeout=(10, timeout), allow_redirects=True)
    response.raise_for_status()
    return response.json()


def normalize_group_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url

    if "/c/Endometriosis/support-group" in url:
        return "https://www.dailystrength.org/group/endometriosis"

    return url.rstrip("/")


def build_group_page_url(base_url: str, page_num: int) -> str:
    base_url = normalize_group_url(base_url)
    if page_num <= 1:
        return base_url
    return f"{base_url}?page={page_num}"


def build_ajax_url(base_url: str, page_num: int, limit: int) -> str:
    base_url = normalize_group_url(base_url)
    return f"{base_url}/discussions/ajax?page={page_num}&limit={limit}"


def to_absolute(base_url: str, href: str) -> str:
    return urljoin(base_url, href or "")


def extract_thread_id_from_url(url: str) -> str:
    match = DISCUSSION_RE.search(url or "")
    if match:
        return match.group(1).strip().lower()
    parsed = urlparse(url or "")
    slug = parsed.path.rstrip("/").split("/")[-1].strip().lower()
    return slug


def extract_user_id_from_profile_url(url: str) -> str:
    match = PROFILE_ID_RE.search(url or "")
    return match.group(1) if match else ""


def build_post_url(thread_url: str, anchor_id: str) -> str:
    if anchor_id:
        return f"{thread_url}#{anchor_id}"
    return thread_url


def parse_date_to_iso(date_text: str) -> str:
    date_text = clean_text(date_text)
    if not date_text:
        return ""

    if YMD_RE.match(date_text):
        return date_text

    if DATE_RE.match(date_text):
        try:
            dt = datetime.strptime(date_text, "%m/%d/%Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return ""

    return ""


def find_thread_title(soup: BeautifulSoup) -> str:
    for sel in ["h1", "title"]:
        el = soup.select_one(sel)
        if el:
            txt = clean_text(el.get_text(" ", strip=True))
            if txt:
                return txt
    return ""


def find_group_meta(soup: BeautifulSoup) -> tuple:
    category_name = "Women's Health"
    category_slug = "endometriosis"

    text = soup.get_text("\n", strip=True)
    if "Women's Health" in text:
        category_name = "Women's Health"

    return category_name, category_slug


def first_non_empty(*values) -> str:
    for value in values:
        if value is None:
            continue
        s = str(value).strip()
        if s:
            return s
    return ""


def normalize_attr_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(x).strip() for x in value if str(x).strip())
    return str(value).strip()


def find_native_ids_from_element(el) -> Dict[str, str]:
    result = {
        "message_id": "",
        "native_post_id": "",
        "anchor_id": "",
        "post_id": "",
        "comment_id": "",
        "reply_to_post_number": "",
        "reply_to_post_id": "",
    }

    if el is None:
        return result

    bad_wrapper_ids = {"main-content", "content", "page", "wrapper", "container", "app", "root"}

    candidates = [el]
    candidates.extend(list(el.parents)[:3])

    for node in candidates:
        if not getattr(node, "attrs", None):
            continue

        node_id = normalize_attr_value(node.attrs.get("id"))
        data_id = normalize_attr_value(node.attrs.get("data-id"))
        data_post_id = normalize_attr_value(node.attrs.get("data-post-id"))
        data_message_id = normalize_attr_value(node.attrs.get("data-message-id"))
        data_comment_id = normalize_attr_value(node.attrs.get("data-comment-id"))
        data_reply_to = normalize_attr_value(node.attrs.get("data-reply-to"))
        data_parent_id = normalize_attr_value(node.attrs.get("data-parent-id"))
        data_target = normalize_attr_value(node.attrs.get("data-target"))

        if node_id in bad_wrapper_ids:
            node_id = ""

        anchor_id = ""
        if node_id:
            anchor_id = node_id
        elif data_target.startswith("#"):
            anchor_id = data_target[1:].strip()

        message_id = first_non_empty(data_message_id, data_post_id, data_comment_id, data_id)
        native_post_id = first_non_empty(data_post_id, data_message_id, data_comment_id, data_id, node_id)
        reply_to_post_id = first_non_empty(data_reply_to, data_parent_id)

        if message_id or native_post_id or anchor_id or reply_to_post_id:
            result["message_id"] = message_id
            result["native_post_id"] = native_post_id
            result["anchor_id"] = anchor_id
            result["reply_to_post_id"] = reply_to_post_id
            return result

    for a in el.find_all("a", href=True):
        href = a.get("href", "").strip()

        if "#" in href and not result["anchor_id"]:
            frag = href.split("#", 1)[1].strip()
            if frag and frag not in bad_wrapper_ids:
                result["anchor_id"] = frag

        match = re.search(r"(?:message|post|comment)[_-]?id[=/:-]?([A-Za-z0-9_-]+)", href, re.I)
        if match and not result["message_id"]:
            result["message_id"] = match.group(1)

    return result


def parse_listing_threads(soup: BeautifulSoup, listing_url: str) -> List[dict]:
    threads = []
    seen = set()

    feed = soup.select_one("ul.discussion-list.newsfeed__feed")
    items = feed.select("li.newsfeed__item") if feed else []

    for item in items:
        title_link = item.select_one("h3.newsfeed__title a[href]")
        if not title_link:
            continue

        href = title_link.get("href", "")
        full_url = to_absolute(listing_url, href)
        thread_id = extract_thread_id_from_url(full_url)
        if not thread_id or thread_id in seen:
            continue

        title = clean_text(title_link.get_text(" ", strip=True))
        if not title:
            continue

        listing_anchor_id = normalize_attr_value(title_link.get("id"))
        data_page = normalize_attr_value(title_link.get("data-page"))

        posted_by_link = item.select_one(".posts__posted-by a[href*='/user/profile/']")
        listing_author = clean_text(posted_by_link.get_text(" ", strip=True)) if posted_by_link else ""
        listing_author_id = extract_user_id_from_profile_url(posted_by_link.get("href", "")) if posted_by_link else ""

        last_reply_link = item.select_one(".posts__last-reply a[href*='/user/profile/']")
        last_message_author = clean_text(last_reply_link.get_text(" ", strip=True)) if last_reply_link else ""
        last_message_author_id = extract_user_id_from_profile_url(last_reply_link.get("href", "")) if last_reply_link else ""

        time_el = item.select_one("time.newsfeed__item-time, .posts__last-reply time[datetime], time[datetime]")
        last_message_date = clean_text(time_el.get("datetime", "")) if time_el else ""

        replies_count = 0
        discuss_btn = item.select_one("a.posts__discuss-btn .newsfeed__icon-count")
        if discuss_btn:
            replies_count = safe_int(discuss_btn.get_text(" ", strip=True), 0)

        likes_count = 0
        like_btn = item.select_one("button .newsfeed__icon-count")
        if like_btn:
            likes_count = safe_int(like_btn.get_text(" ", strip=True), 0)

        preview_el = item.select_one("div.newsfeed__description")
        preview_text = clean_multiline_text(preview_el.get_text("\n", strip=True)) if preview_el else ""

        seen.add(thread_id)
        threads.append({
            "thread_id": thread_id,
            "thread_url_id": thread_id,
            "thread_title": title,
            "thread_title_detail": title,
            "thread_url": full_url,
            "listing_native_id": listing_anchor_id,
            "listing_data_page": data_page,
            "listing_author": listing_author,
            "listing_author_id": listing_author_id,
            "replies_count": replies_count,
            "views_count": None,
            "last_message_date": last_message_date,
            "last_message_author": last_message_author,
            "last_message_author_id": last_message_author_id,
            "last_message_id": "",
            "last_page": 1,
            "thread_pages_count": 1,
            "likes_count_listing": likes_count,
            "preview_text": preview_text,
        })

    return threads


def parse_ajax_listing_threads(ajax_json: dict, base_url: str) -> List[dict]:
    content = ajax_json.get("content", "")
    if not content:
        return []

    soup = BeautifulSoup(content, "html.parser")
    return parse_listing_threads(soup, base_url)


def parse_dailystrength_thread_blocks(soup: BeautifulSoup, thread_url: str, thread_id: str) -> List[dict]:
    posts = []
    candidates = []

    for el in soup.find_all(["div", "section", "article", "li"]):
        txt = clean_multiline_text(el.get_text("\n", strip=True))
        if not txt:
            continue

        if not re.search(r"\b\d{2}/\d{2}/\d{4}\b", txt):
            continue

        if len(txt) > 2500:
            continue
        if "All content posted on this site is the responsibility" in txt:
            continue
        if "Terms| Site Map| Privacy| Legal" in txt:
            continue

        candidates.append(el)

    seen_html = set()
    unique = []
    for el in candidates:
        key = str(el)
        if key in seen_html:
            continue
        seen_html.add(key)
        unique.append(el)

    for el in unique:
        lines = [clean_text(x) for x in el.get_text("\n", strip=True).splitlines()]
        lines = [x for x in lines if x]

        date_idx = -1
        for i, line in enumerate(lines):
            if DATE_RE.match(line):
                date_idx = i
                break

        if date_idx <= 0:
            continue

        author = lines[date_idx - 1]
        date = lines[date_idx]
        if not author or len(author) > 120:
            continue

        body_lines = []
        for x in lines[date_idx + 1:]:
            if x in {"Leave A Reply", "Join the Conversation", "SHOW MORE"}:
                break
            if x in {"0", "1", "2", "3", "4", "5"}:
                continue
            if x.lower() in {"reply", "like", "share"}:
                continue
            body_lines.append(x)

        body = clean_multiline_text("\n".join(body_lines))
        if not body:
            continue
        if body.startswith("Women's Health / Endometriosis Support Group"):
            continue

        native = find_native_ids_from_element(el)

        posts.append({
            "author": author,
            "user_id": author,
            "native_user_id": "",
            "date": date,
            "date_iso": parse_date_to_iso(date),
            "body": body,
            "likes_count": 0,
            "dislikes_count": 0,
            "thread_id": thread_id,
            "message_id": native["message_id"],
            "native_post_id": native["native_post_id"],
            "anchor_id": native["anchor_id"],
            "reply_to_post_number": "",
            "reply_to_post_id": native["reply_to_post_id"],
            "post_url": build_post_url(thread_url, native["anchor_id"]),
        })

    deduped = []
    seen_keys = set()
    for post in posts:
        key = (post["author"], post["date"], post["body"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(post)

    for idx, post in enumerate(deduped, start=1):
        post["post_number"] = idx

    return deduped


def parse_thread_page(session: requests.Session, thread_meta: dict, cfg: dict) -> dict:
    timeout = int(cfg.get("request", {}).get("timeout_seconds", 30))
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))

    thread_url = thread_meta["thread_url"]
    html = fetch_html(session, thread_url, timeout, referer=normalize_group_url(cfg["start_urls"][0]))
    time.sleep(sleep_seconds)

    soup = BeautifulSoup(html, "html.parser")

    thread_id = extract_thread_id_from_url(thread_url) or thread_meta.get("thread_id", "")
    thread_title_detail = find_thread_title(soup) or thread_meta.get("thread_title", "")
    category_name, category_slug = find_group_meta(soup)

    parsed_posts = parse_dailystrength_thread_blocks(soup, thread_url, thread_id)
    if not parsed_posts:
        raise ValueError("No posts found in thread page")

    opening_post = parsed_posts[0]

    for idx, post in enumerate(parsed_posts, start=1):
        post["post_number"] = idx
        post["type"] = "post" if idx == 1 else "comment"
        post["is_original_post"] = (idx == 1)

        if idx == 1:
            post["post_id"] = post.get("native_post_id", "") or post.get("message_id", "")
            post["comment_id"] = ""
        else:
            post["post_id"] = post.get("native_post_id", "") or post.get("message_id", "")
            post["comment_id"] = post.get("native_post_id", "") or post.get("message_id", "")

        post["reply_to_post_number"] = ""
        post["reply_to_post_id"] = post.get("reply_to_post_id", "")
        post["post_url"] = build_post_url(thread_url, post.get("anchor_id", ""))

    replies = parsed_posts[1:]
    last_post = parsed_posts[-1]
    likes_total = sum(safe_int(x.get("likes_count", 0), 0) for x in parsed_posts)

    item = {
        "source_id": cfg["source_id"],
        "source_mode": cfg.get("source_mode", "forum_dailystrength"),
        "thread_id": thread_id,
        "thread_url_id": thread_id,
        "thread_title": thread_meta.get("thread_title", thread_title_detail),
        "thread_title_detail": thread_title_detail,
        "thread_url": thread_url,
        "listing_category": cfg.get("parsing", {}).get("listing_category", "Endometriosis Support Group"),
        "category_id": cfg.get("parsing", {}).get("category_id", None),
        "category_name": category_name,
        "category_slug": category_slug,
        "thread_starter": opening_post.get("author", ""),
        "thread_starter_id": opening_post.get("user_id", ""),
        "opening_post_id": opening_post.get("native_post_id", "") or opening_post.get("message_id", ""),
        "opening_message_id": opening_post.get("message_id", ""),
        "opening_post_date": opening_post.get("date", ""),
        "opening_post_body": opening_post.get("body", ""),
        "listing_author": thread_meta.get("listing_author", "") or opening_post.get("author", ""),
        "listing_author_id": thread_meta.get("listing_author_id", "") or opening_post.get("user_id", ""),
        "replies_count": thread_meta.get("replies_count", len(replies)),
        "views_count": None,
        "last_message_date": thread_meta.get("last_message_date", "") or last_post.get("date", ""),
        "last_message_author": thread_meta.get("last_message_author", "") or last_post.get("author", ""),
        "last_message_author_id": thread_meta.get("last_message_author_id", "") or last_post.get("user_id", ""),
        "last_message_id": last_post.get("message_id", ""),
        "last_page": 1,
        "thread_pages_count": 1,
        "posts_count": 1,
        "comments_count": len(replies),
        "likes_total": likes_total,
        "post": {
            "author": opening_post.get("author", ""),
            "user_id": opening_post.get("user_id", ""),
            "native_user_id": opening_post.get("native_user_id", ""),
            "date": opening_post.get("date", ""),
            "date_iso": opening_post.get("date_iso", ""),
            "body": opening_post.get("body", ""),
            "likes_count": safe_int(opening_post.get("likes_count", 0), 0),
            "dislikes_count": safe_int(opening_post.get("dislikes_count", 0), 0),
            "thread_id": thread_id,
            "message_id": opening_post.get("message_id", ""),
            "native_post_id": opening_post.get("native_post_id", ""),
            "anchor_id": opening_post.get("anchor_id", ""),
            "post_number": 1,
            "type": "post",
            "is_original_post": True,
            "post_id": opening_post.get("post_id", ""),
            "comment_id": "",
            "reply_to_post_number": "",
            "reply_to_post_id": opening_post.get("reply_to_post_id", ""),
            "post_url": opening_post.get("post_url", thread_url),
        },
        "replies": [],
    }

    for reply in replies:
        item["replies"].append({
            "author": reply.get("author", ""),
            "user_id": reply.get("user_id", ""),
            "native_user_id": reply.get("native_user_id", ""),
            "date": reply.get("date", ""),
            "date_iso": reply.get("date_iso", ""),
            "body": reply.get("body", ""),
            "likes_count": safe_int(reply.get("likes_count", 0), 0),
            "dislikes_count": safe_int(reply.get("dislikes_count", 0), 0),
            "thread_id": thread_id,
            "message_id": reply.get("message_id", ""),
            "native_post_id": reply.get("native_post_id", ""),
            "anchor_id": reply.get("anchor_id", ""),
            "post_number": reply.get("post_number", 0),
            "type": "comment",
            "is_original_post": False,
            "post_id": reply.get("post_id", ""),
            "comment_id": reply.get("comment_id", ""),
            "reply_to_post_number": "",
            "reply_to_post_id": reply.get("reply_to_post_id", ""),
            "post_url": reply.get("post_url", thread_url),
        })

    return item


def collect_all_listing_threads(cfg: dict, session: requests.Session, errors_file: Path) -> List[dict]:
    global should_stop

    source_id = cfg["source_id"]
    timeout = int(cfg.get("request", {}).get("timeout_seconds", 30))
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))

    base_url = normalize_group_url(cfg["start_urls"][0])
    ajax_cfg = cfg.get("pagination", {})
    ajax_limit = int(ajax_cfg.get("ajax_limit", 15))
    start_page = int(ajax_cfg.get("start_page", 1))
    max_pages = int(ajax_cfg.get("max_pages", 1000))
    stop_after_empty = int(ajax_cfg.get("stop_after_consecutive_empty_pages", 3))

    discovered_threads = []
    discovered_thread_ids = set()
    consecutive_empty = 0

    for page_num in range(start_page, max_pages + 1):
        if should_stop:
            break

        try:
            if page_num == 1:
                page_url = build_group_page_url(base_url, 1)
                html = fetch_html(session, page_url, timeout, referer=base_url)
                soup = BeautifulSoup(html, "html.parser")
                page_threads = parse_listing_threads(soup, page_url)
                source_label = page_url
            else:
                referer = build_group_page_url(base_url, page_num)
                ajax_url = build_ajax_url(base_url, page_num, ajax_limit)
                ajax_json = fetch_ajax_listing(session, ajax_url, referer=referer, timeout=timeout)
                page_threads = parse_ajax_listing_threads(ajax_json, base_url)
                source_label = ajax_url

            page_count = len(page_threads)
            new_on_page = 0
            skipped_existing_page = 0

            for thread in page_threads:
                thread_id = thread.get("thread_id", "").strip()
                if not thread_id:
                    continue
                if thread_id in discovered_thread_ids:
                    skipped_existing_page += 1
                    continue

                discovered_thread_ids.add(thread_id)
                discovered_threads.append(thread)
                new_on_page += 1

            if new_on_page == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0

            print(f"[INFO] Listing page {page_num}: {source_label}")
            print(
                f"[INFO] page_threads={page_count} "
                f"new_threads={new_on_page} "
                f"skipped_existing={skipped_existing_page} "
                f"collected_total={len(discovered_threads)}"
            )

            if consecutive_empty >= stop_after_empty:
                print(f"[INFO] Stop listing after {consecutive_empty} consecutive empty pages")
                break

        except Exception as exc:
            print(f"[ERROR] Listing error on page {page_num}: {exc}")
            append_jsonl(errors_file, {
                "source_id": source_id,
                "stage": "listing",
                "page_num": page_num,
                "error": str(exc),
                "timestamp": now_iso(),
            })
            consecutive_empty += 1
            if consecutive_empty >= stop_after_empty:
                break

        time.sleep(sleep_seconds)

    return discovered_threads


def scrape_dailystrength_forum(cfg: dict, session: requests.Session, posts_file: Path, errors_file: Path):
    global should_stop

    source_id = cfg["source_id"]
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))

    existing_thread_ids = set()
    if cfg.get("resume", {}).get("enabled", True):
        existing_thread_ids = load_existing_thread_ids(posts_file)

    print(f"[INFO] Starting scraper for {source_id}")
    print(f"[INFO] Resume mode: {cfg.get('resume', {}).get('enabled', True)}")
    print(f"[INFO] Existing thread_ids in output: {len(existing_thread_ids)}")
    print("=" * 80)

    discovered_threads = collect_all_listing_threads(cfg, session, errors_file)

    print("=" * 80)
    print(f"[INFO] Collected thread URLs: {len(discovered_threads)}")
    print("=" * 80)

    processed = 0
    skipped_existing = 0

    for idx, thread_meta in enumerate(discovered_threads, start=1):
        if should_stop:
            break

        thread_id = thread_meta["thread_id"]

        if thread_id in existing_thread_ids:
            skipped_existing += 1
            print(f"[INFO] Thread {idx}/{len(discovered_threads)} SKIP existing thread_id={thread_id}")
            continue

        try:
            item = parse_thread_page(session, thread_meta, cfg)
            append_jsonl(posts_file, item)
            existing_thread_ids.add(thread_id)
            processed += 1

            messages_scraped = 1 + len(item.get("replies", []))
            print(
                f"[INFO] Thread {idx}/{len(discovered_threads)} "
                f"thread_id={thread_id} "
                f"messages/comments scraped={messages_scraped} "
                f"replies={len(item.get('replies', []))}"
            )

        except Exception as exc:
            print(f"[ERROR] Thread error for {thread_id}: {exc}")
            append_jsonl(errors_file, {
                "source_id": source_id,
                "stage": "thread",
                "thread_id": thread_id,
                "thread_url": thread_meta.get("thread_url", ""),
                "error": str(exc),
                "timestamp": now_iso(),
            })

        time.sleep(sleep_seconds)

    print("=" * 80)
    print(f"[INFO] Finished source {source_id}")
    print(f"[INFO] processed_new_threads={processed}")
    print(f"[INFO] skipped_existing={skipped_existing}")
    print(f"[INFO] output_file={posts_file}")
    print(f"[INFO] errors_file={errors_file}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    cfg = load_config(args.config)

    posts_file = Path(cfg["output"]["posts_file"])
    errors_file = Path(cfg["output"]["errors_file"])

    ensure_parent_dir(posts_file)
    ensure_parent_dir(errors_file)

    session = build_session(cfg)

    if cfg.get("source_mode", "") != "forum_dailystrength":
        raise ValueError(f"Unsupported source_mode: {cfg.get('source_mode', '')}")

    scrape_dailystrength_forum(cfg, session, posts_file, errors_file)


if __name__ == "__main__":
    main()
