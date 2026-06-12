#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
The Mighty topic scraper (requests + BeautifulSoup only).

What it does:
- loads cookies from a simple key : value text file
- scrapes visible listing cards from the topic page
- tries to follow real thread URLs when the HTML exposes them
- tries real load-more / next-page controls only when the HTML exposes a real URL
- resume mode: reads existing JSONL, skips existing thread_id, appends only new ones
- writes ONE JSON line per thread, with nested post + replies

Important:
- This scraper does NOT invent important IDs.
- If The Mighty hides the full feed behind private JS/API calls that are not exposed in HTML,
  the scraper will stop gracefully after the visible cards instead of sending a fake AJAX request.
"""

import argparse
import json
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

should_stop = False


def signal_handler(sig, frame):
    global should_stop
    print("\n[WARN] Stop requested. Finishing current thread then exiting safely...")
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
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


# =========================================================
# FILE + TEXT HELPERS
# =========================================================

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_parent_dir(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, obj: dict):
    ensure_parent_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_multiline_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    value = value.replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", x).strip() for x in value.splitlines()]
    lines = [x for x in lines if x]
    return "\n".join(lines).strip()


def safe_int(value, default=0):
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default

    mult = 1
    low = text.lower()
    if low.endswith("k"):
        mult = 1000
        text = text[:-1]
    elif low.endswith("m"):
        mult = 1000000
        text = text[:-1]

    text = text.replace(",", "").strip()
    try:
        if "." in text:
            return int(float(text) * mult)
        return int(text) * mult
    except Exception:
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else default


def now_iso() -> str:
    return datetime.now().isoformat()


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def absolute_url(base_url: str, maybe_url: str) -> str:
    if not maybe_url:
        return ""
    return urljoin(base_url, maybe_url.strip())


def dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# =========================================================
# COOKIES + SESSION
# =========================================================

def load_cookies_file(path: Path) -> Dict[str, str]:
    cookies = {}
    if not path.exists():
        return cookies

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key:
                cookies[key] = value
    return cookies


def build_session(cfg: dict, project_root: Path) -> requests.Session:
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
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    cookies_name = cfg.get("cookies_file", "")
    if cookies_name:
        cookie_path = project_root / cookies_name
        cookies = load_cookies_file(cookie_path)
        if cookies:
            for key, value in cookies.items():
                session.cookies.set(key, value, domain="themighty.com")
                session.cookies.set(key, value, domain=".themighty.com")
            print(f"[INFO] Loaded {len(cookies)} cookies from {cookie_path.name}")
        else:
            print(f"[WARN] Cookies file not found or empty: {cookie_path}")

    return session


# =========================================================
# RESUME MODE
# =========================================================

def load_existing_thread_ids(path: Path) -> Set[str]:
    seen = set()
    if not path.exists():
        return seen

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            tid = clean_text(str(obj.get("thread_id", "")))
            if tid:
                seen.add(tid)
    return seen


# =========================================================
# HTTP
# =========================================================

def fetch_response(session: requests.Session, url: str, cfg: dict, referer: str = "", method: str = "GET", data=None, params=None):
    timeout = int(cfg.get("request", {}).get("timeout_seconds", 30))
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer

    method = method.upper()
    if method == "POST":
        resp = session.post(url, headers=headers, data=data, params=params, timeout=(10, timeout), allow_redirects=True)
    else:
        resp = session.get(url, headers=headers, params=params, timeout=(10, timeout), allow_redirects=True)

    resp.raise_for_status()
    return resp


def fetch_html(session: requests.Session, url: str, cfg: dict, referer: str = "", method: str = "GET", data=None, params=None) -> str:
    resp = fetch_response(session, url, cfg, referer=referer, method=method, data=data, params=params)
    return resp.text


# =========================================================
# DOM HELPERS
# =========================================================

def extract_handle_from_text(text: str) -> str:
    match = re.search(r"@([A-Za-z0-9_.-]+)", text or "")
    return f"@{match.group(1)}" if match else ""


def extract_handle_from_subtree(el: Tag) -> str:
    return extract_handle_from_text(clean_text(el.get_text(" ", strip=True)))


def first_text(el: Tag, selectors: List[str]) -> str:
    for sel in selectors:
        node = el.select_one(sel)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


def attr_first(el: Tag, names: List[str]) -> str:
    for name in names:
        val = el.get(name)
        if val:
            return clean_text(str(val))
    return ""


def remove_unwanted_nodes(node: Tag):
    for sel in [
        "script", "style", "noscript", "svg", "img", "button", "form", "input",
        "textarea", "figure", "figcaption", "iframe", "video", "audio"
    ]:
        for bad in node.select(sel):
            bad.decompose()


def extract_nativeish_id_from_string(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"\bpost[-_:]?(\d+)\b",
        r"\bcomment[-_:]?(\d+)\b",
        r"\bmessage[-_:]?(\d+)\b",
        r"\bthread[-_:]?(\d+)\b",
        r"\bentry[-_:]?(\d+)\b",
        r"\bitem[-_:]?(\d+)\b",
        r"/posts?/(\d+)",
        r"/stories?/(\d+)",
        r"\bpostId[\"']*\s*[:=]\s*[\"']?(\d+)",
        r"\bcommentId[\"']*\s*[:=]\s*[\"']?(\d+)",
        r"\bmessageId[\"']*\s*[:=]\s*[\"']?(\d+)"
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return ""


def extract_nativeish_id(el: Tag) -> str:
    attr_names = [
        "id", "data-id", "data-post-id", "data-comment-id", "data-thread-id",
        "data-message-id", "data-item-id", "data-entry-id", "data-reactid",
        "data-key", "data-testid", "href", "data-url", "data-share-url",
        "data-href", "data-permalink", "aria-controls"
    ]

    for node in [el] + list(el.find_all(True)):
        for attr_name in attr_names:
            val = node.get(attr_name)
            if val:
                found = extract_nativeish_id_from_string(clean_text(str(val)))
                if found:
                    return found
    return ""


def extract_url_candidates(el: Tag, base_url: str) -> List[str]:
    out = []

    for a in el.find_all("a", href=True):
        href = clean_text(a.get("href", ""))
        if not href:
            continue
        full = absolute_url(base_url, href)
        if is_http_url(full):
            out.append(full)

    extra_attrs = [
        "data-url", "data-share-url", "data-copy-link", "data-href",
        "data-permalink", "data-post-url", "data-link", "formaction"
    ]
    for node in [el] + list(el.find_all(True)):
        for attr in extra_attrs:
            val = node.get(attr)
            if val:
                full = absolute_url(base_url, clean_text(str(val)))
                if is_http_url(full):
                    out.append(full)

    return dedupe_keep_order(out)


def choose_best_thread_url(urls: List[str], topic_url: str) -> str:
    if not urls:
        return ""

    bad_parts = [
        "/u/", "/user/", "/author/", "/topic/", "/groupdirectory/", "/dashboard/",
        "/help", "/privacy", "/terms", "open.spotify.com", "corp.themighty.com",
        "facebook.com", "instagram.com", "twitter.com", "x.com"
    ]

    good = []
    fallback = []
    for url in urls:
        low = url.lower()
        if any(part in low for part in bad_parts):
            fallback.append(url)
        else:
            good.append(url)

    if good:
        # Prefer deeper paths over the topic page itself.
        good = sorted(good, key=lambda u: (u.rstrip("/") == topic_url.rstrip("/"), len(urlparse(u).path)))
        return good[-1]

    for url in urls:
        if url.rstrip("/") != topic_url.rstrip("/"):
            return url
    return urls[0]


def extract_date_text(el: Tag) -> str:
    t = el.find("time")
    if t:
        txt = clean_text(t.get_text(" ", strip=True))
        if txt:
            return txt
        dt = clean_text(t.get("datetime", ""))
        if dt:
            return dt

    text = clean_text(el.get_text(" ", strip=True))
    patterns = [
        r"\b\d+\s*(?:s|m|h|d|w|mo|y)\b",
        r"\b(?:today|yesterday)\b",
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b.*?\b\d{4}\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(0)
    return ""


def extract_date_iso(el: Tag) -> str:
    t = el.find("time")
    if not t:
        return ""
    for attr in ["datetime", "pubdate", "dateTime"]:
        value = clean_text(t.get(attr, ""))
        if value:
            return value
    return ""


def extract_visible_counts(el: Tag) -> Tuple[Optional[int], Optional[int]]:
    text = clean_text(el.get_text(" ", strip=True))
    likes_count = None
    comments_count = None

    m_react = re.search(r"(\d[\d,.]*\s*[kKmM]?)\s+reactions?\b", text, re.I)
    if m_react:
        likes_count = safe_int(m_react.group(1), 0)

    m_comment = re.search(r"(\d[\d,.]*\s*[kKmM]?)\s+comments?\b", text, re.I)
    if m_comment:
        comments_count = safe_int(m_comment.group(1), 0)

    if re.search(r"\bBe first\b", text, re.I):
        comments_count = 0

    return likes_count, comments_count


def looks_like_feed_card_text(text: str) -> bool:
    low = text.lower()
    signals = [
        "reactions", "comments", "copy link", "link copied", "save",
        "report post", "block user", "follow post"
    ]
    return any(sig in low for sig in signals)


def find_post_cards(soup: BeautifulSoup) -> List[Tag]:
    cards = []
    seen_keys = set()

    selectors = ["article", "section", "div"]
    for sel in selectors:
        for el in soup.select(sel):
            text = clean_text(el.get_text(" ", strip=True))
            if len(text) < 40:
                continue
            if not looks_like_feed_card_text(text):
                continue

            # avoid huge wrappers by requiring visible counts or visible copy-link/save UI nearby
            likes_count, comments_count = extract_visible_counts(el)
            if likes_count is None and comments_count is None and "copy link" not in text.lower():
                continue

            key = text[:280]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            cards.append(el)

    return cards


def find_title_in_card(card: Tag) -> str:
    for tag_name in ["h1", "h2", "h3", "h4"]:
        node = card.find(tag_name)
        if node:
            title = clean_text(node.get_text(" ", strip=True))
            if title:
                return title

    # fall back to a strong body-like line before reactions/comments
    text = clean_multiline_text(card.get_text("\n", strip=True))
    for line in text.splitlines():
        low = line.lower().strip()
        if not low:
            continue
        if any(x in low for x in ["reactions", "comments", "copy link", "save", "follow", "report post"]):
            continue
        if len(line) >= 8:
            return line[:220]
    return ""


def find_author_in_card(card: Tag) -> Tuple[str, str, str]:
    author = ""
    user_id = ""
    native_user_id = ""

    handle = extract_handle_from_subtree(card)
    if handle:
        user_id = handle
        native_user_id = handle

    # prefer anchors that look like user/profile links or are near the top of the card
    anchors = card.find_all("a", href=True)
    for a in anchors[:12]:
        text = clean_text(a.get_text(" ", strip=True))
        href = clean_text(a.get("href", ""))
        if not text:
            continue
        low = text.lower()
        if low in {"follow", "save", "share", "copy link", "endometriosis"}:
            continue
        if text.startswith("#"):
            continue
        if looks_like_url_only(text):
            continue
        if "/u/" in href or "/user/" in href or "/author/" in href:
            author = text
            if not user_id:
                user_id = text
            native_user_id = href
            break

    if not author:
        # look for visible name above the handle
        text_lines = [clean_text(x) for x in card.get_text("\n", strip=True).splitlines()]
        text_lines = [x for x in text_lines if x]
        for i, line in enumerate(text_lines[:12]):
            if line.startswith("@") and i > 0:
                author = text_lines[i - 1]
                break

    if not author and anchors:
        for a in anchors[:8]:
            text = clean_text(a.get_text(" ", strip=True))
            if not text:
                continue
            if text.startswith("#") or text.startswith("@"):
                continue
            if text.lower() in {"follow", "save", "share", "endometriosis"}:
                continue
            author = text
            break

    if not user_id:
        user_id = author
    if not native_user_id:
        native_user_id = handle or author

    return author, user_id, native_user_id


def looks_like_url_only(text: str) -> bool:
    return bool(re.fullmatch(r"https?://\S+", text or ""))


def extract_body_from_block(block: Tag) -> str:
    work = BeautifulSoup(str(block), "html.parser")
    remove_unwanted_nodes(work)

    candidates = []
    selectors = [
        "main", "article", "section", "div", "p", "blockquote", "li"
    ]
    for sel in selectors:
        for node in work.select(sel):
            text = clean_multiline_text(node.get_text("\n", strip=True))
            if not text:
                continue
            low = text.lower()
            if low in {
                "follow", "save", "share", "copy link", "copy link to share",
                "link copied to clipboard.", "see full photo", "report post", "block user"
            }:
                continue
            if len(text) < 20:
                continue
            candidates.append(text)

    candidates = dedupe_keep_order(candidates)
    if not candidates:
        return ""

    # choose the longest candidate and remove trailing UI lines
    body = max(candidates, key=len)
    junk_lines = {
        "follow", "save", "share", "copy link", "copy link to share",
        "link copied to clipboard.", "see full photo", "report post", "block user",
        "love this", "this is so helpful", "you are not alone", "i’ve been there",
        "sending you energy", "cheering you on!"
    }
    kept = []
    for line in body.splitlines():
        l = line.strip().lower()
        if not l:
            continue
        if l in junk_lines:
            continue
        kept.append(line.strip())
    return "\n".join(kept).strip()


def extract_thread_id(card: Tag, thread_url: str) -> Tuple[str, str]:
    native_id = extract_nativeish_id(card)
    thread_url_id = ""

    if thread_url:
        parsed = urlparse(thread_url)
        path = parsed.path.strip("/")

        # prefer numeric/native id from URL if present
        found = extract_nativeish_id_from_string(thread_url)
        if found:
            thread_url_id = found
        elif path:
            thread_url_id = path.replace("/", "_")

    thread_id = native_id or thread_url_id
    return thread_id, (thread_url_id or thread_id)


# =========================================================
# LISTING PARSE + PAGINATION DISCOVERY
# =========================================================

def parse_listing_cards(html: str, topic_url: str, cfg: dict) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = find_post_cards(soup)
    cards = cards[: int(cfg.get("parsing", {}).get("max_cards_per_page", 500))]

    parsed_items = []
    seen_local_ids = set()

    for card in cards:
        title = find_title_in_card(card)
        author, user_id, native_user_id = find_author_in_card(card)
        date_text = extract_date_text(card)
        date_iso = extract_date_iso(card)
        body = extract_body_from_block(card)
        likes_count, comments_count = extract_visible_counts(card)
        url_candidates = extract_url_candidates(card, topic_url)
        thread_url = choose_best_thread_url(url_candidates, topic_url)
        thread_id, thread_url_id = extract_thread_id(card, thread_url)

        if not thread_id:
            continue
        if thread_id in seen_local_ids:
            continue
        seen_local_ids.add(thread_id)

        if not title:
            title = (body.splitlines()[0][:220] if body else "")
        if not body:
            body = title

        item = {
            "source_id": cfg["source_id"],
            "source_mode": cfg.get("source_mode", "forum_themighty_topic"),
            "thread_id": thread_id,
            "thread_url_id": thread_url_id,
            "thread_title": title,
            "thread_title_detail": title,
            "thread_url": thread_url or topic_url,
            "listing_category": cfg.get("parsing", {}).get("listing_category"),
            "category_id": cfg.get("parsing", {}).get("category_id"),
            "category_name": cfg.get("parsing", {}).get("category_name"),
            "category_slug": cfg.get("parsing", {}).get("category_slug"),
            "thread_starter": author,
            "thread_starter_id": user_id,
            "opening_post_id": thread_id,
            "opening_message_id": thread_id,
            "opening_post_date": date_text,
            "opening_post_body": body,
            "listing_author": author,
            "listing_author_id": user_id,
            "replies_count": comments_count if comments_count is not None else 0,
            "views_count": None,
            "last_message_date": date_text,
            "last_message_author": author,
            "last_message_author_id": user_id,
            "last_message_id": thread_id,
            "last_page": 1,
            "thread_pages_count": 1,
            "posts_count": 1,
            "comments_count": comments_count if comments_count is not None else 0,
            "likes_total": likes_count if likes_count is not None else 0,
            "post": {
                "author": author,
                "user_id": user_id,
                "native_user_id": native_user_id,
                "date": date_text,
                "date_iso": date_iso,
                "body": body,
                "likes_count": likes_count if likes_count is not None else 0,
                "dislikes_count": 0,
                "thread_id": thread_id,
                "message_id": thread_id,
                "native_post_id": thread_id,
                "anchor_id": thread_id,
                "post_number": 1,
                "type": "post",
                "is_original_post": True,
                "post_id": thread_id,
                "comment_id": "",
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": thread_url or topic_url
            },
            "replies": []
        }
        parsed_items.append(item)

    return parsed_items


def discover_next_listing_url(soup: BeautifulSoup, current_url: str, page_num: int) -> str:
    # explicit rel=next or button/form/link with a real URL
    selectors = [
        'link[rel="next"]',
        'a[rel="next"]',
        'a.next',
        'a[aria-label*="next" i]',
        'button[data-url]',
        'button[data-next-url]',
        'a.load-more[href]',
        'button[formaction]'
    ]
    for sel in selectors:
        node = soup.select_one(sel)
        if not node:
            continue
        for attr in ["href", "data-url", "data-next-url", "formaction"]:
            value = clean_text(node.get(attr, ""))
            if value:
                full = absolute_url(current_url, value)
                if is_http_url(full):
                    return full

    # numbered page fallback only if the URL pattern is explicit and different from current URL
    parsed = urlparse(current_url)
    if "/page/" in parsed.path:
        match = re.search(r"/page/(\d+)/?", parsed.path)
        if match:
            next_page = int(match.group(1)) + 1
            next_path = re.sub(r"/page/\d+/?", f"/page/{next_page}/", parsed.path)
            return f"{parsed.scheme}://{parsed.netloc}{next_path}"

    if page_num == 1:
        guess = current_url.rstrip("/") + "/page/2/"
        if guess.rstrip("/") != current_url.rstrip("/"):
            return guess

    return ""


# =========================================================
# THREAD PARSE
# =========================================================

def find_comment_blocks(soup: BeautifulSoup) -> List[Tag]:
    blocks = []
    seen = set()
    selectors = [
        '[data-testid*="comment"]',
        '[id*="comment"]',
        'article', 'section', 'div', 'li'
    ]
    for sel in selectors:
        for el in soup.select(sel):
            text = clean_text(el.get_text(" ", strip=True))
            if len(text) < 15:
                continue
            looks_commentish = (
                "reply" in text.lower() or
                "comment" in text.lower() or
                "love this" in text.lower() or
                "copy link" in text.lower()
            )
            cid = extract_nativeish_id(el)
            if not looks_commentish and not cid:
                continue
            key = text[:200]
            if key in seen:
                continue
            seen.add(key)
            blocks.append(el)
    return blocks


def parse_thread_page(session: requests.Session, item: dict, cfg: dict) -> dict:
    thread_url = item.get("thread_url", "")
    topic_url = cfg.get("topic_url", "")
    if not thread_url or not is_http_url(thread_url):
        return item
    if thread_url.rstrip("/") == topic_url.rstrip("/"):
        return item

    html = fetch_html(session, thread_url, cfg, referer=topic_url)
    soup = BeautifulSoup(html, "html.parser")

    title = first_text(soup, ["h1", "h2", "h3"])
    if title:
        item["thread_title"] = title
        item["thread_title_detail"] = title

    op_author, op_user_id, op_native_user_id = find_author_in_card(soup)
    op_date = extract_date_text(soup)
    op_date_iso = extract_date_iso(soup)
    op_body = extract_body_from_block(soup)
    op_id = extract_nativeish_id(soup) or item["opening_post_id"]
    likes_count, comments_count = extract_visible_counts(soup)

    if op_author:
        item["thread_starter"] = op_author
        item["thread_starter_id"] = op_user_id
        item["listing_author"] = op_author
        item["listing_author_id"] = op_user_id
        item["post"]["author"] = op_author
        item["post"]["user_id"] = op_user_id
        item["post"]["native_user_id"] = op_native_user_id

    if op_date:
        item["opening_post_date"] = op_date
        item["post"]["date"] = op_date
    if op_date_iso:
        item["post"]["date_iso"] = op_date_iso
    if op_body:
        item["opening_post_body"] = op_body
        item["post"]["body"] = op_body
    if likes_count is not None:
        item["post"]["likes_count"] = likes_count
    if op_id:
        item["opening_post_id"] = op_id
        item["opening_message_id"] = op_id
        item["last_message_id"] = op_id
        item["post"]["message_id"] = op_id
        item["post"]["native_post_id"] = op_id
        item["post"]["anchor_id"] = op_id
        item["post"]["post_id"] = op_id

    replies = []
    seen_reply_ids = set()
    opening_post_id = item["opening_post_id"]

    post_number = 2
    for block in find_comment_blocks(soup):
        cid = extract_nativeish_id(block)
        if not cid:
            continue
        if cid == opening_post_id:
            continue
        if cid in seen_reply_ids:
            continue

        author, user_id, native_user_id = find_author_in_card(block)
        body = extract_body_from_block(block)
        if not body:
            continue
        date_text = extract_date_text(block)
        date_iso = extract_date_iso(block)
        reply_likes, _ = extract_visible_counts(block)

        reply = {
            "author": author,
            "user_id": user_id,
            "native_user_id": native_user_id,
            "date": date_text,
            "date_iso": date_iso,
            "body": body,
            "likes_count": reply_likes if reply_likes is not None else 0,
            "dislikes_count": 0,
            "thread_id": item["thread_id"],
            "message_id": cid,
            "native_post_id": cid,
            "anchor_id": cid,
            "post_number": post_number,
            "type": "comment",
            "is_original_post": False,
            "post_id": opening_post_id,
            "comment_id": cid,
            "reply_to_post_number": "",
            "reply_to_post_id": "",
            "post_url": thread_url + f"#comment-{cid}"
        }
        replies.append(reply)
        seen_reply_ids.add(cid)
        post_number += 1

    if replies:
        item["replies"] = replies
        item["comments_count"] = len(replies)
        item["replies_count"] = len(replies)
        item["posts_count"] = 1 + len(replies)
        item["likes_total"] = int(item["post"].get("likes_count", 0) or 0) + sum(int(r.get("likes_count", 0) or 0) for r in replies)
        item["last_message_date"] = replies[-1].get("date", item["opening_post_date"])
        item["last_message_author"] = replies[-1].get("author", "")
        item["last_message_author_id"] = replies[-1].get("user_id", "")
        item["last_message_id"] = replies[-1].get("message_id", opening_post_id)
    else:
        item["replies"] = []
        item["posts_count"] = 1
        if comments_count is not None and item.get("comments_count", 0) == 0:
            item["comments_count"] = comments_count
            item["replies_count"] = comments_count
        item["likes_total"] = int(item["post"].get("likes_count", 0) or 0)
        item["last_message_date"] = item["opening_post_date"]
        item["last_message_author"] = item["thread_starter"]
        item["last_message_author_id"] = item["thread_starter_id"]
        item["last_message_id"] = item["opening_message_id"]

    return item


# =========================================================
# MAIN
# =========================================================

def scrape_themighty(cfg: dict, project_root: Path):
    global should_stop

    source_id = cfg["source_id"]
    topic_url = cfg["topic_url"]
    posts_file = project_root / cfg["output"]["posts_file"]
    errors_file = project_root / cfg["output"]["errors_file"]
    ensure_parent_dir(posts_file)
    ensure_parent_dir(errors_file)

    session = build_session(cfg, project_root)
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))
    max_listing_pages = int(cfg.get("parsing", {}).get("max_listing_pages", 200))
    max_empty_pages = int(cfg.get("parsing", {}).get("stop_after_consecutive_empty_pages", 2))
    try_follow_thread_urls = bool(cfg.get("parsing", {}).get("try_follow_thread_urls", True))

    existing_thread_ids = load_existing_thread_ids(posts_file) if cfg.get("resume", {}).get("enabled", True) else set()

    print(f"[INFO] Starting scraper for {source_id}")
    print(f"[INFO] topic_url={topic_url}")
    print(f"[INFO] resume_enabled={cfg.get('resume', {}).get('enabled', True)}")
    print(f"[INFO] existing_thread_ids={len(existing_thread_ids)}")
    print("=" * 70)

    listing_url = topic_url
    page_num = 1
    consecutive_empty_pages = 0
    visited_listing_urls = set()

    total_page_threads = 0
    total_new_threads = 0
    total_skipped_existing = 0
    total_messages_scraped = 0
    total_comments_scraped = 0

    while listing_url and page_num <= max_listing_pages and not should_stop:
        if listing_url in visited_listing_urls:
            print(f"[WARN] Listing URL already visited, stopping pagination: {listing_url}")
            break
        visited_listing_urls.add(listing_url)

        try:
            html = fetch_html(session, listing_url, cfg, referer=topic_url)
            soup = BeautifulSoup(html, "html.parser")
            items = parse_listing_cards(html, topic_url, cfg)
        except Exception as e:
            print(f"[ERROR] listing fetch failed on page {page_num}: {e}")
            append_jsonl(errors_file, {
                "source_id": source_id,
                "stage": "listing_fetch",
                "listing_url": listing_url,
                "page_num": page_num,
                "error": str(e),
                "timestamp": now_iso()
            })
            break

        page_threads = len(items)
        total_page_threads += page_threads
        page_new_threads = 0
        page_skipped_existing = 0

        print(f"[INFO] listing page number={page_num}")
        print(f"[INFO] page_threads={page_threads}")

        if not items:
            consecutive_empty_pages += 1
        else:
            consecutive_empty_pages = 0

        for item in items:
            if should_stop:
                break

            tid = item["thread_id"]
            if tid in existing_thread_ids:
                total_skipped_existing += 1
                page_skipped_existing += 1
                continue

            try:
                if try_follow_thread_urls and item.get("thread_url") and item.get("thread_url") != topic_url:
                    item = parse_thread_page(session, item, cfg)
            except Exception as e:
                append_jsonl(errors_file, {
                    "source_id": source_id,
                    "stage": "thread_fetch_or_parse",
                    "thread_id": tid,
                    "thread_url": item.get("thread_url", ""),
                    "error": str(e),
                    "timestamp": now_iso()
                })

            append_jsonl(posts_file, item)
            existing_thread_ids.add(tid)

            page_new_threads += 1
            total_new_threads += 1
            total_messages_scraped += 1
            total_comments_scraped += len(item.get("replies", []))

            print(
                f"[INFO] thread_id={tid} "
                f"messages/comments scraped in each thread={1 + len(item.get('replies', []))}/{len(item.get('replies', []))}"
            )

            time.sleep(sleep_seconds)

        print(f"[INFO] new_threads={page_new_threads}")
        print(f"[INFO] skipped_existing={page_skipped_existing}")

        if consecutive_empty_pages >= max_empty_pages:
            print("[INFO] Stopping because listing pages are empty.")
            break

        next_url = discover_next_listing_url(soup, listing_url, page_num)
        if not next_url:
            print("[INFO] No real next-page / load-more URL exposed in HTML. Stopping gracefully.")
            break
        if next_url == listing_url:
            print("[INFO] Next URL is same as current URL. Stopping gracefully.")
            break

        listing_url = next_url
        page_num += 1
        time.sleep(sleep_seconds)

    print("=" * 70)
    print(f"[DONE] total_page_threads={total_page_threads}")
    print(f"[DONE] total_new_threads={total_new_threads}")
    print(f"[DONE] total_skipped_existing={total_skipped_existing}")
    print(f"[DONE] total_messages_scraped={total_messages_scraped}")
    print(f"[DONE] total_comments_scraped={total_comments_scraped}")
    print(f"[DONE] output={posts_file}")
    print(f"[DONE] errors={errors_file}")


def main():
    parser = argparse.ArgumentParser(description="The Mighty topic scraper")
    parser.add_argument("--config", default="configs/SRC011.json", help="Path to JSON config")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    project_root = config_path.parent.parent
    cfg = load_config(str(config_path))
    scrape_themighty(cfg, project_root)


if __name__ == "__main__":
    main()
