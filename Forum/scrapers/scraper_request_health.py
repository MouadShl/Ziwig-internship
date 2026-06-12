import os
import re
import json
import time
import html
import argparse
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "configs", "SRC014.json")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_parent_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    ensure_parent_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_existing_thread_ids(output_file: str) -> Set[str]:
    existing = set()
    if not os.path.exists(output_file):
        return existing

    with open(output_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                thread_id = str(row.get("thread_id") or "").strip()
                if thread_id:
                    existing.add(thread_id)
            except Exception:
                continue
    return existing


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = value.replace("\xa0", " ")
    value = value.replace("\r", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def clean_body_text(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def extract_number(text_value: Any) -> Optional[int]:
    if not text_value:
        return None
    text_value = str(text_value).replace(",", "")
    m = re.search(r"(\d+)", text_value)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def extract_like_count(text_value: Any) -> int:
    if not text_value:
        return 0
    s = str(text_value)
    m = re.search(r"\((\d+)\)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\b", s)
    if m:
        return int(m.group(1))
    return 0


def normalize_url(url: str) -> str:
    return (url or "").split("#")[0].strip()


def extract_thread_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/posts/(?:private/)?(\d+)", url)
    return m.group(1) if m else None


def walk_json(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_json(item)


def dedupe_by_key(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        val = str(row.get(key) or "").strip()
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(row)
    return out


def parse_cookies_file(cookies_file: str) -> Tuple[Dict[str, str], str]:
    cookies = {}
    if not cookies_file or not os.path.exists(cookies_file):
        return cookies, ""

    with open(cookies_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            if ":" in line and "\t" not in line and not line.startswith("#"):
                name, value = line.split(":", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    cookies[name] = value
                continue

            if line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[5].strip()
                value = parts[6].strip()
                if name:
                    cookies[name] = value

    raw_cookie_header = "; ".join([f"{k}={v}" for k, v in cookies.items()])
    return cookies, raw_cookie_header


def session_from_config(config: Dict[str, Any]) -> requests.Session:
    session = requests.Session()

    base_headers = config.get("headers") or {}
    if base_headers:
        session.headers.update(base_headers)

    cookies, _ = parse_cookies_file(config.get("cookies_file", ""))
    for name, value in cookies.items():
        session.cookies.set(name, value)

    return session


def fetch_text(session: requests.Session, url: str, timeout: int, headers: Optional[Dict[str, str]] = None) -> str:
    resp = session.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.text


def fetch_json(
    session: requests.Session,
    url: str,
    timeout: int,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    resp = session.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.json()


def soup_from_html(html_text: str) -> BeautifulSoup:
    return BeautifulSoup(html_text, "html.parser")


def html_to_text(node) -> str:
    if node is None:
        return ""
    return clean_body_text(node.get_text("\n", strip=False))


def find_next_data_json(html_text: str) -> Optional[Dict[str, Any]]:
    patterns = [
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r"<script[^>]+id='__NEXT_DATA__'[^>]*>(.*?)</script>",
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, re.S | re.I)
        if m:
            raw = m.group(1).strip()
            try:
                return json.loads(raw)
            except Exception:
                pass
    return None


def build_thread_url(community_slug: str, post_id: str, url_encoded_title: str = "", is_private: bool = False) -> str:
    if is_private:
        path = f"/{community_slug}/posts/private/{post_id}"
    else:
        path = f"/{community_slug}/posts/{post_id}"

    if url_encoded_title:
        path += f"/{url_encoded_title}"

    return urljoin("https://healthunlocked.com", path)


def map_listing_api_item(item: Dict[str, Any], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    post_id = item.get("postId") or item.get("id")
    if post_id is None:
        return None

    author = item.get("author") or {}
    user_id = author.get("userId") or author.get("id")
    username = clean_text(author.get("username") or author.get("name"))
    thread_id = str(post_id)
    url_encoded_title = clean_text(item.get("urlEncodedTitle") or item.get("slug") or "")
    is_private = bool(item.get("isPrivate"))

    return {
        "source_id": config["source_id"],
        "source_mode": config["source_mode"],
        "thread_id": thread_id,
        "thread_url_id": thread_id,
        "thread_title": clean_text(item.get("title")),
        "thread_title_detail": clean_text(item.get("bodySnippet") or item.get("snippet") or ""),
        "thread_url": build_thread_url(
            community_slug=config.get("community_slug", "endometriosis-uk"),
            post_id=thread_id,
            url_encoded_title=url_encoded_title,
            is_private=is_private,
        ),
        "listing_category": config.get("listing_category"),
        "category_id": config.get("category_id"),
        "category_name": config.get("category_name"),
        "category_slug": config.get("category_slug"),
        "thread_starter": username,
        "thread_starter_id": str(user_id) if user_id is not None else "",
        "listing_author": username,
        "listing_author_id": str(user_id) if user_id is not None else "",
        "opening_post_date": item.get("dateCreated") or item.get("createdAt") or "",
        "replies_count": int(item.get("totalResponses") or item.get("repliesCount") or 0),
        "views_count": None,
        "is_private": is_private,
        "_cursor_post_id": thread_id,
    }


def extract_listing_posts_from_next_data(next_data: Optional[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not next_data:
        return []

    rows = []
    for node in walk_json(next_data):
        if not isinstance(node, dict):
            continue
        if "postId" not in node:
            continue
        if "title" not in node:
            continue
        if "dateCreated" not in node:
            continue

        row = map_listing_api_item(node, config)
        if row:
            rows.append(row)

    return dedupe_by_key(rows, "thread_id")


def parse_listing_page(html_text: str, page_url: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    soup = soup_from_html(html_text)
    next_data = find_next_data_json(html_text)

    rows_from_json = extract_listing_posts_from_next_data(next_data, config)
    rows_from_json_map = {row["thread_id"]: row for row in rows_from_json}

    rows = []
    for card in soup.select('[data-testid="results-post"]'):
        link = card.select_one('a[href*="/posts/"]')
        if not link:
            continue

        href = link.get("href", "").strip()
        thread_url = urljoin(page_url, href)
        thread_id = extract_thread_id_from_url(thread_url)
        if not thread_id:
            continue

        title_el = card.select_one("h3")
        user_link = card.select_one('a[href^="/user/"]')
        time_el = card.select_one("time[datetime]")
        snippet_el = card.select_one('[data-sentry-element="ResultsPostBody"]')

        replies_count = 0
        for a in card.select('a[href*="#responses"]'):
            replies_count = extract_number(a.get_text(" ", strip=True)) or 0
            break

        json_row = rows_from_json_map.get(thread_id, {})

        rows.append(
            {
                "source_id": config["source_id"],
                "source_mode": config["source_mode"],
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_title": clean_text(title_el.get_text(" ", strip=True) if title_el else json_row.get("thread_title")),
                "thread_title_detail": clean_text(snippet_el.get_text(" ", strip=True) if snippet_el else json_row.get("thread_title_detail")),
                "thread_url": normalize_url(json_row.get("thread_url") or thread_url),
                "listing_category": config.get("listing_category"),
                "category_id": config.get("category_id"),
                "category_name": config.get("category_name"),
                "category_slug": config.get("category_slug"),
                "thread_starter": clean_text(user_link.get_text(" ", strip=True) if user_link else json_row.get("thread_starter")),
                "thread_starter_id": clean_text(json_row.get("thread_starter_id")),
                "listing_author": clean_text(user_link.get_text(" ", strip=True) if user_link else json_row.get("listing_author")),
                "listing_author_id": clean_text(json_row.get("listing_author_id")),
                "opening_post_date": time_el.get("datetime") if time_el else (json_row.get("opening_post_date") or ""),
                "replies_count": replies_count if replies_count else int(json_row.get("replies_count") or 0),
                "views_count": None,
                "is_private": bool(json_row.get("is_private")),
                "_cursor_post_id": thread_id,
            }
        )

    if not rows:
        rows = rows_from_json

    return dedupe_by_key(rows, "thread_id")


def build_api_headers(config: Dict[str, Any]) -> Dict[str, str]:
    _, raw_cookie_header = parse_cookies_file(config.get("cookies_file", ""))

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": config["start_url"],
        "User-Agent": (config.get("headers") or {}).get(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    if raw_cookie_header:
        headers["Cookie"] = raw_cookie_header

    return headers


def fetch_listing_batch_api(
    session: requests.Session,
    config: Dict[str, Any],
    timeout: int,
    created_before_post_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    api_url = config["api_latest_url"]
    if created_before_post_id:
        api_url = f"{api_url}?createdBeforePostId={created_before_post_id}"

    api_headers = build_api_headers(config)

    print(f"[api] cursor={created_before_post_id or 'FIRST'} cookies={len(session.cookies.get_dict())}")

    data = fetch_json(session, api_url, timeout=timeout, headers=api_headers)

    posts = []

    if isinstance(data, list):
        posts = data
    elif isinstance(data, dict):
        posts = data.get("posts") or data.get("communityPosts") or []
        if isinstance(posts, dict):
            posts = posts.get("posts") or []
        elif not isinstance(posts, list):
            posts = []
    else:
        posts = []

    rows = []
    for item in posts:
        if not isinstance(item, dict):
            continue
        row = map_listing_api_item(item, config)
        if row:
            rows.append(row)

    print(f"[api] fetched_posts={len(rows)}")
    return dedupe_by_key(rows, "thread_id")


def extract_opening_post_json(next_data: Optional[Dict[str, Any]], thread_id: str) -> Dict[str, Any]:
    if not next_data:
        return {}

    candidates = []
    for node in walk_json(next_data):
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if str(node_id) != str(thread_id):
            continue
        if "body" in node or "dateCreated" in node or "author" in node:
            candidates.append(node)

    if not candidates:
        return {}

    candidates.sort(
        key=lambda x: (
            1 if x.get("body") else 0,
            len(str(x.get("body") or "")),
        ),
        reverse=True,
    )
    return candidates[0]


def extract_reply_rows_from_next_data(next_data: Optional[Dict[str, Any]], thread_id: str) -> List[Dict[str, Any]]:
    if not next_data:
        return []

    rows = []
    seen_ids = set()

    for node in walk_json(next_data):
        if not isinstance(node, dict):
            continue

        node_id = node.get("id")
        author = node.get("author")
        body = node.get("body")
        date_created = node.get("dateCreated")
        post_url = node.get("postUrl") or ""
        order_val = str(node.get("order") or "")

        if node_id is None:
            continue
        if not isinstance(author, dict):
            continue
        if body is None:
            continue
        if date_created is None:
            continue

        if str(node_id) == str(thread_id):
            continue

        looks_like_reply = False
        if f"responses={node_id}" in str(post_url):
            looks_like_reply = True
        elif order_val and str(thread_id) in order_val:
            looks_like_reply = True
        elif node.get("numRatings") is not None and author.get("username"):
            looks_like_reply = True

        if not looks_like_reply:
            continue

        message_id = str(node_id)
        if message_id in seen_ids:
            continue
        seen_ids.add(message_id)

        author_id = author.get("id")

        rows.append(
            {
                "message_id": message_id,
                "native_post_id": message_id,
                "comment_id": message_id,
                "post_id": message_id,
                "author": clean_text(author.get("username")),
                "user_id": str(author_id) if author_id is not None else "",
                "native_user_id": str(author_id) if author_id is not None else "",
                "date_iso": date_created,
                "date": date_created,
                "body": clean_body_text(body),
                "likes_count": int(node.get("numRatings") or 0),
                "post_url": normalize_url(post_url),
                "thread_id": str(thread_id),
            }
        )

    rows.sort(key=lambda x: (x.get("date_iso") or "", x.get("message_id") or ""))
    return rows


def extract_reply_rows_from_dom(soup: BeautifulSoup, thread_id: str, thread_url: str) -> List[Dict[str, Any]]:
    rows = []
    seen = set()

    for item in soup.select('[data-sentry-component="InteractiveReplyItem"]'):
        author_link = item.select_one('a[href^="/user/"]')
        time_el = item.select_one("time[datetime]")

        body_parts = []
        for p in item.select("p"):
            txt = clean_body_text(p.get_text("\n", strip=False))
            if txt:
                body_parts.append(txt)
        body = "\n".join(body_parts).strip()

        author = clean_text(author_link.get_text(" ", strip=True) if author_link else "")
        user_href = author_link.get("href", "") if author_link else ""
        user_slug = user_href.rsplit("/", 1)[-1] if user_href else ""
        date_iso = time_el.get("datetime") if time_el else ""

        like_button = item.select_one('button[data-testid="like-button"]')
        likes_count = extract_like_count(like_button.get_text(" ", strip=True) if like_button else "")

        synthetic_key = f"{author}|{date_iso}|{body[:100]}"
        if synthetic_key in seen:
            continue
        seen.add(synthetic_key)

        rows.append(
            {
                "message_id": "",
                "native_post_id": "",
                "comment_id": "",
                "post_id": "",
                "author": author,
                "user_id": user_slug,
                "native_user_id": "",
                "date_iso": date_iso,
                "date": date_iso,
                "body": body,
                "likes_count": likes_count,
                "post_url": thread_url,
                "thread_id": str(thread_id),
            }
        )

    rows.sort(key=lambda x: (x.get("date_iso") or "", x.get("author") or ""))
    return rows


def parse_thread_page(
    html_text: str,
    thread_url: str,
    listing_row: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    soup = soup_from_html(html_text)
    next_data = find_next_data_json(html_text)

    canonical_el = soup.select_one('link[rel="canonical"]')
    canonical_url = normalize_url(canonical_el.get("href", "").strip()) if canonical_el else normalize_url(thread_url)
    thread_id = extract_thread_id_from_url(canonical_url) or str(listing_row["thread_id"])

    title_el = soup.select_one('[data-testid="post-heading"]')
    thread_title = clean_text(title_el.get_text(" ", strip=True) if title_el else listing_row.get("thread_title"))

    published_meta = soup.select_one('meta[property="article:published_time"]')
    opening_body_el = soup.select_one('[data-testid="post-body"]')

    opening_json = extract_opening_post_json(next_data, thread_id)
    opening_author_json = opening_json.get("author") or {}

    thread_starter = clean_text(
        opening_author_json.get("username")
        or listing_row.get("thread_starter")
        or listing_row.get("listing_author")
    )

    thread_starter_id = ""
    if opening_author_json.get("id") is not None:
        thread_starter_id = str(opening_author_json.get("id"))
    elif listing_row.get("thread_starter_id"):
        thread_starter_id = clean_text(listing_row.get("thread_starter_id"))

    opening_post_id = str(opening_json.get("id") or thread_id)
    opening_message_id = str(opening_json.get("id") or thread_id)
    opening_post_date = (
        opening_json.get("dateCreated")
        or (published_meta.get("content") if published_meta else "")
        or listing_row.get("opening_post_date")
        or ""
    )
    opening_post_body = clean_body_text(opening_json.get("body") or html_to_text(opening_body_el))

    opening_like_button = soup.select_one('button[data-testid="like-button"]')
    opening_likes = extract_like_count(opening_like_button.get_text(" ", strip=True) if opening_like_button else "")

    replies_count_dom = 0
    for a in soup.select('a[href*="#responses"]'):
        replies_count_dom = extract_number(a.get_text(" ", strip=True)) or 0
        break

    reply_rows = extract_reply_rows_from_next_data(next_data, thread_id)
    if not reply_rows:
        reply_rows = extract_reply_rows_from_dom(soup, thread_id, canonical_url)

    normalized_replies = []
    seen_reply_ids = set()

    for idx, reply in enumerate(reply_rows, start=2):
        message_id = clean_text(reply.get("message_id"))
        native_post_id = clean_text(reply.get("native_post_id")) or message_id
        post_id = clean_text(reply.get("post_id")) or message_id or native_post_id
        comment_id = clean_text(reply.get("comment_id")) or post_id
        author = clean_text(reply.get("author"))
        user_id = clean_text(reply.get("user_id")) or author
        native_user_id = clean_text(reply.get("native_user_id")) or user_id
        date_iso = clean_text(reply.get("date_iso") or reply.get("date"))
        body = clean_body_text(reply.get("body"))
        post_url = clean_text(reply.get("post_url")) or canonical_url

        dedupe_key = message_id or f"{author}|{date_iso}|{body[:100]}"
        if dedupe_key in seen_reply_ids:
            continue
        seen_reply_ids.add(dedupe_key)

        normalized_replies.append(
            {
                "author": author,
                "user_id": user_id,
                "native_user_id": native_user_id,
                "date": date_iso,
                "date_iso": date_iso,
                "body": body,
                "likes_count": int(reply.get("likes_count") or 0),
                "dislikes_count": 0,
                "thread_id": str(thread_id),
                "message_id": message_id,
                "native_post_id": native_post_id,
                "anchor_id": comment_id or message_id or native_post_id,
                "post_number": idx,
                "type": "comment",
                "is_original_post": False,
                "post_id": post_id,
                "comment_id": comment_id,
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": post_url,
            }
        )

    last_message_author = thread_starter
    last_message_author_id = thread_starter_id
    last_message_date = opening_post_date
    last_message_id = opening_message_id

    if normalized_replies:
        last_reply = normalized_replies[-1]
        last_message_author = last_reply["author"]
        last_message_author_id = last_reply["user_id"]
        last_message_date = last_reply["date_iso"] or last_reply["date"]
        last_message_id = last_reply["message_id"] or last_reply["post_id"]

    replies_count = len(normalized_replies)
    if replies_count_dom > replies_count:
        replies_count = replies_count_dom

    likes_total = opening_likes + sum(int(x.get("likes_count") or 0) for x in normalized_replies)

    opening_post_user_id = thread_starter_id or thread_starter

    row = {
        "source_id": config["source_id"],
        "source_mode": config["source_mode"],
        "thread_id": str(thread_id),
        "thread_url_id": str(thread_id),
        "thread_title": thread_title,
        "thread_title_detail": clean_text(listing_row.get("thread_title_detail")),
        "thread_url": canonical_url,
        "listing_category": listing_row.get("listing_category"),
        "category_id": listing_row.get("category_id"),
        "category_name": listing_row.get("category_name"),
        "category_slug": listing_row.get("category_slug"),
        "thread_starter": thread_starter,
        "thread_starter_id": thread_starter_id,
        "opening_post_id": opening_post_id,
        "opening_message_id": opening_message_id,
        "opening_post_date": opening_post_date,
        "opening_post_body": opening_post_body,
        "listing_author": listing_row.get("listing_author") or thread_starter,
        "listing_author_id": listing_row.get("listing_author_id") or thread_starter_id,
        "replies_count": replies_count,
        "views_count": None,
        "last_message_date": last_message_date,
        "last_message_author": last_message_author,
        "last_message_author_id": last_message_author_id,
        "last_message_id": last_message_id,
        "last_page": 1,
        "thread_pages_count": 1,
        "posts_count": 1 + len(normalized_replies),
        "comments_count": len(normalized_replies),
        "likes_total": likes_total,
        "post": {
            "author": thread_starter,
            "user_id": opening_post_user_id,
            "native_user_id": thread_starter_id or opening_post_user_id,
            "date": opening_post_date,
            "date_iso": opening_post_date,
            "body": opening_post_body,
            "likes_count": opening_likes,
            "dislikes_count": 0,
            "thread_id": str(thread_id),
            "message_id": opening_message_id,
            "native_post_id": opening_post_id,
            "anchor_id": opening_post_id,
            "post_number": 1,
            "type": "post",
            "is_original_post": True,
            "post_id": opening_post_id,
            "comment_id": "",
            "reply_to_post_number": "",
            "reply_to_post_id": "",
            "post_url": canonical_url
        },
        "replies": normalized_replies
    }

    return row


def write_error(error_file: str, source_id: str, url: str, error: str, context: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "source_id": source_id,
        "url": url,
        "error": str(error),
        "context": context or {},
        "ts": int(time.time()),
    }
    append_jsonl(error_file, payload)


def main(config_path: str = DEFAULT_CONFIG_PATH) -> None:
    config = load_json(config_path)

    output_file = os.path.join(BASE_DIR, config["output_file"])
    error_file = os.path.join(BASE_DIR, config["error_file"])

    ensure_parent_dir(output_file)
    ensure_parent_dir(error_file)

    session = session_from_config(config)
    timeout = int(config.get("request_timeout", 40))
    sleep_seconds = float(config.get("sleep_seconds", 1.0))
    max_listing_batches = int(config.get("max_listing_batches", 100000))
    stop_after_consecutive_no_new_batches = int(config.get("stop_after_consecutive_no_new_batches", 5))

    existing_thread_ids = load_existing_thread_ids(output_file) if config.get("resume_mode", True) else set()
    print(f"[resume] existing_thread_ids={len(existing_thread_ids)}")

    batch_no = 1
    next_cursor = None
    consecutive_no_new_batches = 0

    while batch_no <= max_listing_batches:
        batch_rows = []

        try:
            if batch_no == 1:
                html_text = fetch_text(session, config["start_url"], timeout=timeout)
                batch_rows = parse_listing_page(html_text, config["start_url"], config)

                if not batch_rows:
                    batch_rows = fetch_listing_batch_api(
                        session=session,
                        config=config,
                        timeout=timeout,
                        created_before_post_id=None,
                    )
            else:
                batch_rows = fetch_listing_batch_api(
                    session=session,
                    config=config,
                    timeout=timeout,
                    created_before_post_id=next_cursor,
                )

        except Exception as e:
            write_error(
                error_file,
                config["source_id"],
                config["start_url"] if batch_no == 1 else f"{config['api_latest_url']}?createdBeforePostId={next_cursor}",
                f"listing_fetch_or_parse_failed: {e}",
                {"batch_no": batch_no, "cursor": next_cursor},
            )
            print(f"[listing] batch={batch_no} failed error={e}")
            break

        page_threads = len(batch_rows)
        if page_threads == 0:
            print(f"[listing] batch={batch_no} page_threads=0 new_threads=0 skipped_existing=0")
            print("[stop] no more posts")
            break

        new_rows = []
        skipped_existing = 0

        for row in batch_rows:
            thread_id = str(row.get("thread_id") or "").strip()
            if not thread_id:
                continue

            if thread_id in existing_thread_ids:
                skipped_existing += 1
                continue

            new_rows.append(row)

        print(
            f"[listing] batch={batch_no} "
            f"page_threads={page_threads} "
            f"new_threads={len(new_rows)} "
            f"skipped_existing={skipped_existing}"
        )

        if not new_rows:
            consecutive_no_new_batches += 1
        else:
            consecutive_no_new_batches = 0

        for row in new_rows:
            thread_url = row["thread_url"]
            thread_id = row["thread_id"]

            try:
                thread_html = fetch_text(session, thread_url, timeout=timeout)
                thread_payload = parse_thread_page(thread_html, thread_url, row, config)
                append_jsonl(output_file, thread_payload)
                existing_thread_ids.add(thread_id)

                print(
                    f"[thread] thread_id={thread_id} "
                    f"messages_comments_scraped={1 + len(thread_payload.get('replies', []))}"
                )

            except Exception as e:
                write_error(
                    error_file,
                    config["source_id"],
                    thread_url,
                    f"thread_fetch_or_parse_failed: {e}",
                    {"thread_id": thread_id, "batch_no": batch_no},
                )
                print(f"[thread] thread_id={thread_id} failed error={e}")

            time.sleep(sleep_seconds)

        last_row = batch_rows[-1]
        next_cursor = str(
            last_row.get("_cursor_post_id")
            or last_row.get("thread_id")
            or ""
        ).strip()

        if not next_cursor:
            print("[stop] missing next cursor")
            break

        if consecutive_no_new_batches >= stop_after_consecutive_no_new_batches:
            print(f"[stop] consecutive_no_new_batches={consecutive_no_new_batches}")
            break

        batch_no += 1
        time.sleep(sleep_seconds)

    print("[done]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config JSON")
    args = parser.parse_args()
    main(config_path=args.config)