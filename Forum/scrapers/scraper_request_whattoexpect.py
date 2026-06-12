#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import html as ihtml
import json
import math
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


STOP = False


def handle_stop(sig, frame):
    global STOP
    STOP = True
    print("\n[INFO] Stop requested, finishing current request...")


signal.signal(signal.SIGINT, handle_stop)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


MONTH_RE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
DATE_PATTERNS = [
    rf"{MONTH_RE}\s+\d{{1,2}},\s+\d{{4}}(?:\s+at\s+\d{{1,2}}:\d{{2}}\s*[APMapm\.]*)?",
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
    r"\d{4}-\d{2}-\d{2}",
]


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x]
    return "\n".join(lines).strip()


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", clean_text(text)).strip()


def safe_int(value, default=0) -> int:
    if value is None:
        return default
    raw = re.sub(r"[^\d]", "", str(value))
    return int(raw) if raw else default


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, obj: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_seen_thread_ids(path: Path) -> Set[str]:
    seen: Set[str] = set()
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
            thread_id = str(obj.get("thread_id", "")).strip()
            if thread_id:
                seen.add(thread_id)
    return seen


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def fetch_html(session: requests.Session, url: str, timeout: int, referer: str = "") -> str:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    response = session.get(url, headers=headers, timeout=(10, timeout), allow_redirects=True)
    response.raise_for_status()
    return response.text


def build_listing_url(seed_url: str, page_num: int) -> str:
    parsed = urlparse(seed_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page_num)]
    query = urlencode(qs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


def build_thread_page_url(thread_url: str, page_num: int) -> str:
    thread_url = thread_url.split("#")[0]
    if page_num <= 1:
        return thread_url
    parsed = urlparse(thread_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page_num)]
    query = urlencode(qs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


def parse_jsonish(value: str) -> dict:
    raw = value or ""
    raw = ihtml.unescape(raw).strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    raw = re.sub(r"\s+", " ", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def parse_schema_nodes(soup: BeautifulSoup) -> List[dict]:
    nodes: List[dict] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                nodes.append(cur)
                for v in cur.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                for v in cur:
                    if isinstance(v, (dict, list)):
                        stack.append(v)
    return nodes


def extract_country_scope(soup: BeautifulSoup) -> str:
    for node in parse_schema_nodes(soup):
        if str(node.get("@type", "")).lower() == "organization":
            address = node.get("address") or {}
            if isinstance(address, dict):
                country = one_line(str(address.get("addressCountry", "")))
                if country:
                    return country
    return "Global"


def detect_language(soup: BeautifulSoup, texts: List[str]) -> str:
    if soup.html and soup.html.get("lang"):
        lang = soup.html.get("lang", "").lower().strip()
        if lang.startswith("fr"):
            return "French"
        if lang.startswith("en"):
            return "English"
    blob = " " + " ".join([one_line(x).lower() for x in texts if x]) + " "
    fr_hits = sum(1 for w in [" le ", " la ", " les ", " des ", " est ", " avec ", " pour ", " je ", " pas "] if w in blob)
    en_hits = sum(1 for w in [" the ", " and ", " with ", " for ", " is ", " my ", " i ", " not "] if w in blob)
    if fr_hits > en_hits and fr_hits >= 2:
        return "French"
    return "English"


def get_group_name(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    return one_line(h1.get_text(" ", strip=True)) if h1 else ""


def get_group_category(soup: BeautifulSoup) -> str:
    for node in parse_schema_nodes(soup):
        if str(node.get("@type", "")).lower() == "breadcrumblist":
            items = node.get("itemListElement") or []
            for item in items:
                inner = item.get("item") or {}
                name = one_line(str(inner.get("name", "")))
                if name and name.lower() not in {"community"}:
                    return name
    return ""


def extract_thread_id_from_url(url: str) -> str:
    url = (url or "").split("#")[0]
    for pattern in [
        r"(?:-|/)(\d{4,})(?:\.html|[-/?#]|$)",
        r"[?&](?:id|thread|topic|discussion)=([A-Za-z0-9_-]+)",
    ]:
        m = re.search(pattern, url, re.I)
        if m:
            return m.group(1)
    return ""


def extract_thread_url_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    leaf = path.split("/")[-1] if path else ""
    return leaf[:-5] if leaf.endswith(".html") else leaf


def parse_listing_author_line(text: str) -> Tuple[str, str]:
    text = one_line(text)
    if not text:
        return "", ""
    m = re.match(r"^(.*?)\s*\|\s*by\s+(.+)$", text, re.I)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", text


def parse_latest_line(text: str) -> Tuple[str, str]:
    text = one_line(text)
    if not text:
        return "", ""
    text = re.sub(r"^Latest:\s*", "", text, flags=re.I)
    m = re.match(r"^(.*?)\s*\|\s*(.+)$", text, re.I)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", ""


def extract_total_pages_from_listing(soup: BeautifulSoup) -> Optional[int]:
    list_holder = soup.select_one(".group-discussions__list")
    if list_holder and list_holder.get("data-config"):
        cfg = parse_jsonish(list_holder.get("data-config"))
        total_count = safe_int(cfg.get("totalCount"), 0)
        page_size = safe_int(cfg.get("pageSize"), 0)
        if total_count > 0 and page_size > 0:
            return int(math.ceil(total_count / float(page_size)))
    max_page = 0
    for a in soup.select(".simple-pagination a[href*='page=']"):
        href = a.get("href", "")
        qs = parse_qs(urlparse(href).query)
        if "page" in qs:
            max_page = max(max_page, safe_int(qs["page"][0], 0))
    return max_page or None


def parse_listing_page(soup: BeautifulSoup, listing_url: str) -> Tuple[List[dict], dict]:
    group_name = get_group_name(soup) or "Endometriosis Ladies"
    group_category = get_group_category(soup) or group_name
    group_slug = re.sub(r"\.html$", "", urlparse(listing_url).path.rstrip("/").split("/")[-1])
    total_pages = extract_total_pages_from_listing(soup)
    total_discussions = None
    list_holder = soup.select_one(".group-discussions__list")
    if list_holder and list_holder.get("data-config"):
        holder_cfg = parse_jsonish(list_holder.get("data-config"))
        total_discussions = safe_int(holder_cfg.get("totalCount"), 0) or None

    items: List[dict] = []
    seen_ids: Set[str] = set()
    for item in soup.select(".group-discussions__list__item.__topic"):
        cfg = parse_jsonish(item.get("data-config", ""))
        link = item.select_one("a.linkDiscussion[href]") or item.select_one("a[href]")
        if not link:
            continue
        thread_url = urljoin(listing_url, link.get("href", "")).split("#")[0]
        thread_id = str(cfg.get("id") or "").strip() or extract_thread_id_from_url(thread_url)
        if not thread_id or thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)

        title_node = item.select_one(".group-discussions__list__item__title span") or item.select_one(
            ".group-discussions__list__item__title"
        )
        thread_title = one_line(title_node.get_text(" ", strip=True)) if title_node else one_line(link.get_text(" ", strip=True))
        author_name, listing_date = parse_listing_author_line(
            item.select_one(".group-discussions__list__item__author").get_text(" ", strip=True)
            if item.select_one(".group-discussions__list__item__author")
            else ""
        )
        latest_box = item.select_one(".group-discussions__list__item__latest")
        latest_author, latest_date = parse_latest_line(
            latest_box.select_one(".group-discussions__list__item__latest__c").get_text(" ", strip=True)
            if latest_box and latest_box.select_one(".group-discussions__list__item__latest__c")
            else (latest_box.get_text(" ", strip=True) if latest_box else "")
        )
        replies_count = safe_int(cfg.get("replyCount"), 0)
        visible_comment_node = latest_box.select_one(".group-discussions__list__item__latest__comments span") if latest_box else None
        if visible_comment_node:
            replies_count = max(replies_count, safe_int(visible_comment_node.get_text(" ", strip=True), 0))
        likes_visible = extract_reactions_total(latest_box) if latest_box else 0

        items.append(
            {
                "thread_id": thread_id,
                "thread_url_id": extract_thread_url_id(thread_url),
                "thread_title": thread_title,
                "thread_url": thread_url,
                "listing_author": author_name,
                "listing_author_id": str(cfg.get("authorId") or "") if cfg.get("authorId") is not None else author_name,
                "listing_author_native_id": str(cfg.get("authorId") or ""),
                "listing_date": listing_date,
                "listing_latest_author": latest_author,
                "listing_latest_date": latest_date,
                "replies_count_visible": replies_count,
                "views_count_visible": None,
                "likes_count_visible": likes_visible,
                "group_name": group_name,
                "group_category": group_category,
                "group_slug": group_slug,
                "total_discussions_visible": total_discussions,
                "total_pages_visible": total_pages,
            }
        )
    meta = {
        "group_name": group_name,
        "group_category": group_category,
        "group_slug": group_slug,
        "total_pages": total_pages,
        "total_discussions": total_discussions,
    }
    return items, meta


def epoch_ms_to_iso(ms_value: str) -> str:
    if not ms_value:
        return ""
    try:
        ms_int = int(str(ms_value).strip())
        if ms_int > 9999999999:
            dt = datetime.fromtimestamp(ms_int / 1000.0, tz=timezone.utc)
        else:
            dt = datetime.fromtimestamp(ms_int, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return ""


def parse_visible_date_to_iso(text: str) -> str:
    raw = one_line(text)
    if not raw:
        return ""
    normalized = (
        raw.replace(" a.m.", " AM")
        .replace(" p.m.", " PM")
        .replace(" a.m", " AM")
        .replace(" p.m", " PM")
        .replace(" am", " AM")
        .replace(" pm", " PM")
    )
    for fmt in [
        "%b %d, %Y at %I:%M %p",
        "%B %d, %Y at %I:%M %p",
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ]:
        try:
            dt = datetime.strptime(normalized, fmt)
            if normalized.endswith("Z"):
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return ""


def parse_date_from_node(node) -> Tuple[str, str]:
    if not node:
        return "", ""
    text = one_line(node.get_text(" ", strip=True))
    iso = parse_visible_date_to_iso(text)
    if not iso:
        iso = epoch_ms_to_iso(node.get("data-date", ""))
    return text, iso


def clean_message_body(node) -> str:
    if not node:
        return ""
    clone = BeautifulSoup(str(node), "html.parser")
    for bad in clone.select("script, style, noscript, form, button, svg, .visually-hidden, .screen-reader-only"):
        bad.decompose()
    text = clean_text(clone.get_text("\n", strip=True))
    return text


def extract_reactions_total(container) -> int:
    if not container:
        return 0
    block = container.select_one(".all-user-reactions[data-reactions]")
    if not block:
        return 0
    raw = ihtml.unescape(block.get("data-reactions", "")).strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except Exception:
        return 0
    total = 0
    if isinstance(data, dict):
        for value in data.values():
            total += safe_int(value, 0)
    return total


def extract_author_id_from_classes(node, prefix: str) -> str:
    if not node:
        return ""
    classes = node.get("class") or []
    for cls in classes:
        m = re.match(rf"{re.escape(prefix)}(\d+)$", cls)
        if m:
            return m.group(1)
    for desc in node.find_all(True):
        classes = desc.get("class") or []
        for cls in classes:
            m = re.match(rf"{re.escape(prefix)}(\d+)$", cls)
            if m:
                return m.group(1)
    return ""


def parse_opening_from_schema(soup: BeautifulSoup) -> dict:
    for node in parse_schema_nodes(soup):
        typ = str(node.get("@type", "")).lower()
        if typ not in {"discussionforumposting", "socialmediaposting", "article", "blogposting"}:
            continue
        author = node.get("author") or {}
        author_name = one_line(author.get("name", "")) if isinstance(author, dict) else one_line(str(author))
        headline = one_line(str(node.get("headline", "")))
        body = clean_text(str(node.get("text") or node.get("articleBody") or ""))
        date = one_line(str(node.get("datePublished") or node.get("dateCreated") or ""))
        interaction_stats = node.get("interactionStatistic") or []
        replies_count = 0
        likes_count = 0
        for stat in interaction_stats:
            action = stat.get("interactionType") or {}
            action_type = str(action.get("type", "")).lower() if isinstance(action, dict) else str(action).lower()
            count = safe_int(stat.get("userInteractionCount"), 0)
            if "commentaction" in action_type:
                replies_count = count
            elif "likeaction" in action_type:
                likes_count = count
        return {
            "headline": headline,
            "author": author_name,
            "body": body,
            "date": date,
            "date_iso": parse_visible_date_to_iso(date),
            "replies_count": replies_count,
            "likes_count": likes_count,
        }
    return {}


def extract_thread_pages_count(soup: BeautifulSoup, thread_url: str) -> int:
    max_page = 1
    replies_list = soup.select_one(".discussion-replies__list")
    if replies_list and replies_list.get("data-config"):
        cfg = parse_jsonish(replies_list.get("data-config"))
        total_count = safe_int(cfg.get("totalCount"), 0)
        page_size = safe_int(cfg.get("pageSize"), 0)
        if total_count > 0 and page_size > 0:
            max_page = max(max_page, int(math.ceil(total_count / float(page_size))))
    for a in soup.select(".simple-pagination a[href]"):
        href = urljoin(thread_url, a.get("href", ""))
        qs = parse_qs(urlparse(href).query)
        if "page" in qs:
            max_page = max(max_page, safe_int(qs["page"][0], 1))
    return max_page


def get_opening_post(soup: BeautifulSoup, thread_meta: dict) -> dict:
    schema = parse_opening_from_schema(soup)
    post_section = soup.select_one("section.discussion-original-post") or soup.select_one(".discussion-original-post")
    title = one_line(
        (post_section.select_one(".discussion-original-post__title") if post_section else None).get_text(" ", strip=True)
        if post_section and post_section.select_one(".discussion-original-post__title")
        else ""
    ) or schema.get("headline") or thread_meta.get("thread_title", "")

    author_node = post_section.select_one(".discussion-original-post__author__name") if post_section else None
    author = one_line(author_node.get_text(" ", strip=True)) if author_node else ""
    author = author or schema.get("author") or thread_meta.get("listing_author", "")

    date_node = post_section.select_one(".discussion-original-post__author__updated") if post_section else None
    date_display, date_iso = parse_date_from_node(date_node)
    if not date_display:
        date_display = schema.get("date", "")
    if not date_iso:
        date_iso = schema.get("date_iso", "")

    content_node = post_section.select_one(".discussion-original-post__content") if post_section else None
    body_node = post_section.select_one(".discussion-original-post__content .__messageContent") if post_section else None
    body = clean_message_body(body_node) or schema.get("body", "")

    anchor_id = one_line(content_node.get("id", "")) if content_node else ""
    message_id = re.sub(r"^message-", "", anchor_id) if anchor_id else ""
    reaction_holder = post_section.select_one(".reactions-block[data-post]") if post_section else None
    native_post_id = one_line(reaction_holder.get("data-post", "")) if reaction_holder else ""
    if not native_post_id:
        native_post_id = message_id

    native_user_id = extract_author_id_from_classes(post_section, "topic-by-author-") if post_section else ""

    likes_count = extract_reactions_total(post_section) if post_section else 0
    if likes_count == 0:
        likes_count = safe_int(schema.get("likes_count"), 0)

    if not title or not author or not body:
        return {}

    return {
        "title": title,
        "author": author,
        "user_id": native_user_id or author,
        "native_user_id": native_user_id,
        "date": date_display,
        "date_iso": date_iso,
        "body": body,
        "likes_count": likes_count,
        "message_id": message_id,
        "native_post_id": native_post_id,
        "anchor_id": anchor_id,
        "post_url": thread_meta["thread_url"] + (f"#{anchor_id}" if anchor_id else ""),
        "replies_count_visible": safe_int(schema.get("replies_count"), 0),
    }


def get_reply_nodes(soup: BeautifulSoup) -> List:
    nodes = soup.select(".discussion-replies__list .wte-reply[id^='message-']")
    if not nodes:
        nodes = [n for n in soup.select(".wte-reply[id^='message-']") if not n.find_parent(class_="discussion-original-post")]
    return nodes


def parse_reply_node(node, thread_id: str, thread_url: str) -> Optional[dict]:
    cfg = parse_jsonish(node.get("data-config", ""))
    author = one_line(node.select_one(".wte-reply__author__name").get_text(" ", strip=True)) if node.select_one(".wte-reply__author__name") else ""
    native_user_id = str(cfg.get("authorId") or "").strip() or extract_author_id_from_classes(node, "comment-by-author-")
    date_node = node.select_one(".wte-reply__author__updated")
    date_display, date_iso = parse_date_from_node(date_node)
    body_node = node.select_one(".wte-reply__content__message")
    body = clean_message_body(body_node)
    if not author or not body:
        return None
    anchor_id = one_line(node.get("id", ""))
    message_id = str(cfg.get("id") or "").strip() or re.sub(r"^message-", "", anchor_id)
    reply_to_name = one_line(node.select_one(".wte-reply__content__reply-to").get_text(" ", strip=True)) if node.select_one(".wte-reply__content__reply-to") else ""
    reply_to_name = reply_to_name.lstrip("@").rstrip(",")
    likes_count = extract_reactions_total(node)
    return {
        "author": author,
        "user_id": native_user_id or author,
        "native_user_id": native_user_id,
        "date": date_display,
        "date_iso": date_iso,
        "body": body,
        "likes_count": likes_count,
        "dislikes_count": 0,
        "thread_id": thread_id,
        "message_id": message_id,
        "native_post_id": message_id,
        "anchor_id": anchor_id,
        "reply_to_name": reply_to_name,
        "post_url": thread_url + (f"#{anchor_id}" if anchor_id else ""),
    }


def parse_thread(
    session: requests.Session,
    thread_meta: dict,
    timeout: int,
    sleep_seconds: float,
    errors_file: Path,
    source_id: str,
) -> Optional[dict]:
    thread_url = thread_meta["thread_url"]
    first_html = fetch_html(session, thread_url, timeout, referer=thread_meta.get("listing_url", ""))
    first_soup = BeautifulSoup(first_html, "html.parser")

    opening = get_opening_post(first_soup, thread_meta)
    if not opening:
        raise RuntimeError("opening post not found")

    thread_title = opening["title"]
    thread_pages_count = extract_thread_pages_count(first_soup, thread_url)
    country_scope = extract_country_scope(first_soup)
    language = detect_language(first_soup, [thread_title, opening["body"]])

    replies: List[dict] = []
    seen_reply_ids: Set[str] = set()
    for page_num in range(1, thread_pages_count + 1):
        if STOP:
            break
        if page_num == 1:
            soup = first_soup
        else:
            page_url = build_thread_page_url(thread_url, page_num)
            html = fetch_html(session, page_url, timeout, referer=thread_url)
            soup = BeautifulSoup(html, "html.parser")
            time.sleep(sleep_seconds)
        for node in get_reply_nodes(soup):
            row = parse_reply_node(node, thread_meta["thread_id"], thread_url)
            if not row:
                continue
            dedupe_key = row["message_id"] or row["anchor_id"] or f"{row['author']}|{row['date']}|{row['body'][:120]}"
            if dedupe_key in seen_reply_ids:
                continue
            seen_reply_ids.add(dedupe_key)
            replies.append(row)

    visible_replies = thread_meta.get("replies_count_visible") or opening.get("replies_count_visible") or 0
    if visible_replies and not replies:
        append_jsonl(
            errors_file,
            {
                "source_id": source_id,
                "thread_id": thread_meta["thread_id"],
                "thread_url": thread_meta["thread_url"],
                "error": f"visible replies count is {visible_replies} but no reply blocks were found in HTML",
                "timestamp": datetime.now().isoformat(),
            },
        )

    post_obj = {
        "author": opening["author"],
        "user_id": opening["user_id"],
        "native_user_id": opening["native_user_id"],
        "date": opening["date"],
        "date_iso": opening["date_iso"],
        "body": opening["body"],
        "likes_count": opening["likes_count"],
        "dislikes_count": 0,
        "thread_id": thread_meta["thread_id"],
        "message_id": opening["message_id"],
        "native_post_id": opening["native_post_id"],
        "anchor_id": opening["anchor_id"],
        "post_number": 1,
        "type": "post",
        "is_original_post": True,
        "post_id": opening["native_post_id"],
        "comment_id": "",
        "reply_to_post_number": "",
        "reply_to_post_id": "",
        "post_url": opening["post_url"],
    }

    reply_items: List[dict] = []
    likes_total = safe_int(opening["likes_count"], 0)
    for idx, reply in enumerate(replies, start=2):
        likes_total += safe_int(reply.get("likes_count"), 0)
        reply_items.append(
            {
                "author": reply["author"],
                "user_id": reply["user_id"],
                "native_user_id": reply["native_user_id"],
                "date": reply["date"],
                "date_iso": reply["date_iso"],
                "body": reply["body"],
                "likes_count": reply["likes_count"],
                "dislikes_count": reply.get("dislikes_count", 0),
                "thread_id": thread_meta["thread_id"],
                "message_id": reply["message_id"],
                "native_post_id": reply["native_post_id"],
                "anchor_id": reply["anchor_id"],
                "post_number": idx,
                "type": "comment",
                "is_original_post": False,
                "post_id": reply["native_post_id"],
                "comment_id": reply["message_id"],
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": reply["post_url"],
            }
        )

    last_author = opening["author"]
    last_author_id = opening["user_id"]
    last_date = opening["date"]
    last_message_id = opening["message_id"]
    if reply_items:
        last = reply_items[-1]
        last_author = last["author"]
        last_author_id = last["user_id"]
        last_date = last["date"]
        last_message_id = last["message_id"]

    item = {
        "source_id": source_id,
        "source_mode": "forum_whattoexpect",
        "thread_id": thread_meta["thread_id"],
        "thread_url_id": thread_meta["thread_url_id"],
        "thread_title": thread_title,
        "thread_title_detail": thread_title,
        "thread_url": thread_meta["thread_url"],
        "listing_category": thread_meta.get("group_category") or thread_meta.get("group_name") or "",
        "category_id": None,
        "category_name": thread_meta.get("group_name") or "",
        "category_slug": thread_meta.get("group_slug") or "",
        "thread_starter": opening["author"],
        "thread_starter_id": opening["user_id"],
        "opening_post_id": opening["native_post_id"],
        "opening_message_id": opening["message_id"],
        "opening_post_date": opening["date"],
        "opening_post_body": opening["body"],
        "listing_author": thread_meta.get("listing_author") or opening["author"],
        "listing_author_id": thread_meta.get("listing_author_id") or opening["user_id"],
        "replies_count": safe_int(thread_meta.get("replies_count_visible"), len(reply_items)) or len(reply_items),
        "views_count": thread_meta.get("views_count_visible"),
        "last_message_date": last_date,
        "last_message_author": last_author,
        "last_message_author_id": last_author_id,
        "last_message_id": last_message_id,
        "last_page": thread_pages_count,
        "thread_pages_count": thread_pages_count,
        "posts_count": 1 + len(reply_items),
        "comments_count": len(reply_items),
        "likes_total": likes_total,
        "language": language,
        "country_scope": country_scope,
        "post": post_obj,
        "replies": reply_items,
    }
    return item


def output_paths(cfg: dict) -> Tuple[Path, Path]:
    out_dir = Path(cfg.get("output", {}).get("dir", f"outputs/{cfg['source_id']}"))
    ensure_dir(out_dir)
    source_id = cfg["source_id"]
    return (
        out_dir / f"{source_id}_post_and_comment_final.jsonl",
        out_dir / f"{source_id}_errors_final.jsonl",
    )


def scrape(cfg: dict):
    source_id = cfg["source_id"]
    timeout = int(cfg.get("request", {}).get("timeout_seconds", 30))
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))
    start_listing_page = int(cfg.get("pagination", {}).get("start_listing_page", 1))
    max_listing_pages = int(cfg.get("pagination", {}).get("max_listing_pages", 0))
    stop_after_empty_pages = int(cfg.get("pagination", {}).get("stop_after_empty_pages", 2))

    data_file, errors_file = output_paths(cfg)
    session = make_session()
    seen_thread_ids = load_seen_thread_ids(data_file)

    print(f"[INFO] Resume mode: existing_threads={len(seen_thread_ids)}")

    for seed_url in cfg.get("start_urls", []):
        page_num = start_listing_page
        empty_pages = 0
        discovered_last_page: Optional[int] = None
        while True:
            if STOP:
                break
            if max_listing_pages > 0 and page_num > max_listing_pages:
                break
            if discovered_last_page is not None and page_num > discovered_last_page:
                break

            listing_url = build_listing_url(seed_url, page_num)
            print(f"[INFO] Listing page {page_num}: {listing_url}")

            try:
                html = fetch_html(session, listing_url, timeout, referer="https://community.whattoexpect.com/")
                soup = BeautifulSoup(html, "html.parser")
                page_threads, page_meta = parse_listing_page(soup, listing_url)
                if page_meta.get("total_pages"):
                    discovered_last_page = page_meta["total_pages"]

                new_threads = [t for t in page_threads if t["thread_id"] not in seen_thread_ids]
                skipped_existing = len(page_threads) - len(new_threads)
                visible_discussions = page_meta.get("total_discussions") or "unknown"
                print(
                    f"[INFO] page_threads={len(page_threads)} "
                    f"new_threads={len(new_threads)} "
                    f"skipped_existing={skipped_existing} "
                    f"visible_discussions={visible_discussions}"
                )

                if not page_threads:
                    empty_pages += 1
                    if empty_pages >= stop_after_empty_pages:
                        break
                    page_num += 1
                    time.sleep(sleep_seconds)
                    continue
                empty_pages = 0

                for thread_meta in new_threads:
                    if STOP:
                        break
                    try:
                        thread_meta["listing_url"] = listing_url
                        item = parse_thread(session, thread_meta, timeout, sleep_seconds, errors_file, source_id)
                        if not item:
                            continue
                        append_jsonl(data_file, item)
                        seen_thread_ids.add(item["thread_id"])
                        print(
                            f"[INFO] messages/comments scraped={item['posts_count']} "
                            f"comments={item['comments_count']} "
                            f"thread_id={item['thread_id']}"
                        )
                    except Exception as e:
                        print(f"[ERROR] Thread error: {e}")
                        append_jsonl(
                            errors_file,
                            {
                                "source_id": source_id,
                                "thread_id": thread_meta.get("thread_id", ""),
                                "thread_url": thread_meta.get("thread_url", ""),
                                "error": str(e),
                                "timestamp": datetime.now().isoformat(),
                            },
                        )
                    time.sleep(sleep_seconds)

                page_num += 1
                time.sleep(sleep_seconds)
            except Exception as e:
                print(f"[ERROR] Listing error on page {page_num}: {e}")
                append_jsonl(
                    errors_file,
                    {
                        "source_id": source_id,
                        "listing_url": listing_url,
                        "page_num": page_num,
                        "error": str(e),
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                empty_pages += 1
                if empty_pages >= stop_after_empty_pages:
                    break
                page_num += 1
                time.sleep(sleep_seconds)

    print(f"[DONE] Data file: {data_file}")
    print(f"[DONE] Errors file: {errors_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()
    cfg = load_json(args.config)
    scrape(cfg)


if __name__ == "__main__":
    main()
