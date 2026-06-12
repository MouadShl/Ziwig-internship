import json
import logging
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime
from hashlib import sha1
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://www.inspire.com"


def load_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def normalize_space(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def html_to_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    soup = BeautifulSoup(str(value), "html.parser")
    text = soup.get_text("\n", strip=True)
    return clean_body(text)


def clean_body(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = text.replace("\xa0", " ")
    text = re.sub(r"<!---->", " ", text)
    lines = [normalize_space(x) for x in re.split(r"[\r\n]+", text)]
    out = []
    junk = [
        r"^reply$",
        r"^report$",
        r"^share$",
        r"^follow$",
        r"^show more$",
        r"^see more$",
        r"^read more$",
        r"^view profile$",
        r"^new reply.*$",
        r"^in reply to.*has been deleted$"
    ]
    for line in lines:
        if not line:
            continue
        if any(re.match(p, line, flags=re.I) for p in junk):
            continue
        out.append(line)
    return normalize_space(" ".join(out))


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def stable_thread_url_id(url: str) -> Optional[str]:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if "discussion" in parts:
        i = parts.index("discussion")
        if i + 1 < len(parts):
            return parts[i + 1]
    if "topic" in parts:
        i = parts.index("topic")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else None


def parse_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    text = str(text).replace(",", " ")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([km]?)", text, flags=re.I)
    if not m:
        return None
    n = float(m.group(1))
    s = m.group(2).lower()
    if s == "k":
        n *= 1000
    elif s == "m":
        n *= 1000000
    return int(n)


def looks_blocked(status_code: int, html: str) -> bool:
    lower = (html or "").lower()
    markers = [
        "cloudflare",
        "attention required",
        "access denied",
        "forbidden",
        "captcha",
        "verify you are human"
    ]
    return status_code in (401, 403, 429, 503) or any(x in lower for x in markers)


def jsonl_append(path: str, row: Dict) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_existing_jsonl(path: str) -> List[Dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def existing_thread_ids(path: str) -> Set[str]:
    ids = set()
    for row in read_existing_jsonl(path):
        tid = normalize_space(row.get("thread_id"))
        if tid:
            ids.add(tid)
    return ids


def upsert_thread_record(path: str, record: Dict) -> None:
    tid = normalize_space(record.get("thread_id"))
    rows = read_existing_jsonl(path)
    written = False
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            row_tid = normalize_space(row.get("thread_id"))
            if tid and row_tid == tid:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written = True
            else:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if not written:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def first_text(node: Tag, selectors: List[str], attr: Optional[str] = None) -> Optional[str]:
    for selector in selectors:
        hit = node.select_one(selector)
        if not hit:
            continue
        if attr and hit.has_attr(attr):
            val = normalize_space(hit.get(attr))
            if val:
                return val
        if hit.name == "meta":
            val = normalize_space(hit.get("content"))
        else:
            val = normalize_space(hit.get_text(" ", strip=True))
        if val:
            return val
    return None


def first_tag(node: Tag, selectors: List[str]) -> Optional[Tag]:
    for selector in selectors:
        hit = node.select_one(selector)
        if hit:
            return hit
    return None


def sum_reactions(items: Optional[List[Dict]]) -> int:
    total = 0
    for item in items or []:
        try:
            total += int(item.get("count") or 0)
        except Exception:
            pass
    return total


def recurse(value) -> Iterable[Dict]:
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from recurse(v)
    elif isinstance(value, list):
        for v in value:
            yield from recurse(v)


class InspireScraper:
    def __init__(self, config: Dict):
        self.config = deepcopy(config)
        self.source_id = config["source_id"]
        self.source_mode = config["source_mode"]
        self.start_url = config["start_url"]
        self.html_seed_file = config.get("html_seed_file")
        self.output_file = config["output_file"]
        self.error_file = config["error_file"]
        self.output_dir = config["output_dir"]
        self.delay = float(config.get("request_delay_seconds", 1.0))
        self.timeout = int(config.get("timeout_seconds", 25))
        self.force_rescrape_existing = bool(config.get("force_rescrape_existing", False))
        self.selectors = config.get("selectors") or {}
        self.allowed_domains = set(config.get("allowed_domains") or [])

        ensure_dir(self.output_dir)
        self.session = requests.Session()
        self.session.headers.update(config.get("headers") or {})
        self.done_thread_ids = existing_thread_ids(self.output_file)
        logging.info(
            "resume_loaded existing_threads=%s force_rescrape_existing=%s output=%s",
            len(self.done_thread_ids),
            self.force_rescrape_existing,
            self.output_file,
        )

    def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        if not self.allowed_domains:
            return True
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    def log_error(self, url: str, error_type: str, message: str, status_code: Optional[int] = None) -> None:
        jsonl_append(
            self.error_file,
            {
                "source_id": self.source_id,
                "url": url,
                "error_type": error_type,
                "status_code": status_code,
                "message": message,
                "logged_at": datetime.utcnow().isoformat() + "Z"
            }
        )
        logging.error("type=%s status=%s url=%s message=%s", error_type, status_code, url, message)

    def fetch_html(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            self.log_error(url, "request_error", str(e), None)
            return None
        if looks_blocked(r.status_code, r.text[:12000]):
            self.log_error(url, "blocked_or_forbidden", "blocked, forbidden, or anti-bot html", r.status_code)
            return None
        if r.status_code >= 400:
            self.log_error(url, "http_error", f"http status {r.status_code}", r.status_code)
            return None
        return r.text

    def extract_server_state(self, soup: BeautifulSoup) -> Optional[Dict]:
        script = soup.select_one("script#serverApp-state[type='application/json']")
        if script:
            raw = script.string or script.get_text(strip=True)
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    pass
        return None

    def load_listing_html(self) -> Optional[str]:
        if self.html_seed_file and os.path.exists(self.html_seed_file):
            return read_text(self.html_seed_file)
        return self.fetch_html(self.start_url)

    def extract_listing_items_from_dom(self, html: str) -> List[Dict]:
        soup = BeautifulSoup(html, "lxml")
        items = []
        seen = set()

        for a in soup.select("a[href*='/groups/endometriosis/discussion/']"):
            href = a.get("href")
            if not href:
                continue
            thread_url = canonical_url(urljoin(BASE_URL, href))
            if thread_url in seen:
                continue
            seen.add(thread_url)

            card = a
            for parent in a.parents:
                if isinstance(parent, Tag) and (
                    parent.name in ("ins-anon-post", "ins-post", "article")
                    or parent.has_attr("data-post-url")
                ):
                    card = parent
                    break

            title = normalize_space(a.get_text(" ", strip=True))
            if not title:
                title_tag = card.select_one("a#post-title-link, .pb-title a, a[href*='/discussion/']")
                if title_tag:
                    title = normalize_space(title_tag.get_text(" ", strip=True))

            author = None
            date_text = None
            replies_count = 0
            reactions_count = 0
            views_count = None

            meta_spans = card.select(".pb-meta span")
            for span in meta_spans:
                txt = normalize_space(span.get_text(" ", strip=True))
                if not txt:
                    continue
                if author is None and not re.search(r"\b(repl(?:y|ies)|reaction|view)\b", txt, flags=re.I):
                    author = txt
                    continue
                if re.search(r"\brepl(?:y|ies)\b", txt, flags=re.I):
                    parsed = parse_int(txt)
                    if parsed is not None:
                        replies_count = parsed
                elif re.search(r"\breaction", txt, flags=re.I):
                    parsed = parse_int(txt)
                    if parsed is not None:
                        reactions_count = parsed
                elif re.search(r"\bview", txt, flags=re.I):
                    parsed = parse_int(txt)
                    if parsed is not None:
                        views_count = parsed

            stamp = card.select_one(".pb-stamp, .stamp, .date, .timestamp")
            if stamp:
                date_text = normalize_space(stamp.get_text(" ", strip=True))

            body = None
            body_tag = card.select_one("#post-snippet, .pb-desc, .pb-body, .content, .body")
            if body_tag:
                body = clean_body(body_tag.get_text("\n", strip=True))

            native_thread_id = None
            for attr in ("data-post-id", "data-id", "id"):
                if isinstance(card, Tag) and card.has_attr(attr):
                    native_thread_id = normalize_space(card.get(attr))
                    if native_thread_id:
                        break

            thread_id = native_thread_id or stable_thread_url_id(thread_url)

            items.append(
                {
                    "thread_id": thread_id,
                    "thread_url_id": stable_thread_url_id(thread_url),
                    "thread_url": thread_url,
                    "thread_title": title,
                    "thread_title_detail": title,
                    "listing_category": "Endometriosis",
                    "category_id": None,
                    "category_name": "Endometriosis",
                    "category_slug": "endometriosis",
                    "thread_starter": author,
                    "thread_starter_id": author,
                    "listing_author": author,
                    "listing_author_id": author,
                    "opening_post_id": thread_id,
                    "opening_message_id": thread_id,
                    "opening_post_date": date_text,
                    "opening_post_body": body,
                    "replies_count": replies_count,
                    "views_count": views_count,
                    "likes_total_visible": reactions_count
                }
            )

        return items

    def extract_message_user(self, node: Tag, selectors: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        tag = first_tag(node, selectors)
        if not tag:
            return None, None, None
        name = normalize_space(tag.get_text(" ", strip=True))
        native_user_id = None
        for attr in ("data-user-id", "data-author-id", "data-member-id", "data-id"):
            if tag.has_attr(attr):
                native_user_id = normalize_space(tag.get(attr))
                break
        if not native_user_id and tag.has_attr("href"):
            parts = [p for p in urlparse(tag.get("href")).path.split("/") if p]
            if parts:
                native_user_id = parts[-1]
        return name, native_user_id or name, native_user_id

    def extract_message_date(self, node: Tag, selectors: List[str]) -> Tuple[Optional[str], Optional[str]]:
        for selector in selectors:
            tag = node.select_one(selector)
            if not tag:
                continue
            date_iso = None
            for attr in ("datetime", "content", "title", "aria-label"):
                if tag.has_attr(attr):
                    date_iso = normalize_space(tag.get(attr))
                    if date_iso:
                        break
            date_text = normalize_space(tag.get_text(" ", strip=True)) or date_iso
            if date_text or date_iso:
                return date_text, date_iso
        return None, None

    def extract_node_id(self, node: Tag) -> Tuple[Optional[str], Optional[str]]:
        native_id = None
        anchor_id = normalize_space(node.get("id")) if node.has_attr("id") else None
        for attr in ("data-comment-id", "data-post-id", "data-message-id", "data-id", "id"):
            if node.has_attr(attr):
                native_id = normalize_space(node.get(attr))
                if native_id:
                    break
        return native_id, anchor_id

    def extract_parent_id(self, node: Tag) -> Optional[str]:
        for attr in ("data-parent-id", "data-parent", "data-reply-to", "data-replyto", "data-in-reply-to"):
            if node.has_attr(attr):
                val = normalize_space(node.get(attr))
                if val:
                    return val
        return None

    def extract_reaction_count_from_text(self, node: Tag) -> int:
        txt = normalize_space(node.get_text(" ", strip=True)) or ""
        m = re.search(r"(\d+)\s+Reactions?\b", txt, flags=re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s+reaction", txt, flags=re.I)
        if m:
            return int(m.group(1))
        return 0

    def thread_messages_from_state(self, soup: BeautifulSoup, thread_url: str, thread_title: Optional[str]) -> List[Dict]:
        state = self.extract_server_state(soup)
        if not state:
            return []

        msgs = []
        seen = set()
        for obj in recurse(state):
            if not isinstance(obj, dict):
                continue
            author = obj.get("author") or {}
            content = obj.get("content") or obj.get("text") or obj.get("articleBody") or obj.get("description")
            created = obj.get("created") or obj.get("datePublished") or obj.get("dateCreated") or obj.get("dateModified")
            if not content or not created:
                continue
            if not isinstance(author, dict) and not isinstance(author, str):
                continue

            obj_url = obj.get("url")
            if obj_url:
                full = canonical_url(urljoin(BASE_URL, str(obj_url)))
                if "/discussion/" in full and full != thread_url:
                    continue

            title = normalize_space(obj.get("title"))
            if title and thread_title and title != thread_title and obj_url and "/discussion/" in str(obj_url):
                continue

            author_name = normalize_space(author.get("nickname")) if isinstance(author, dict) else normalize_space(author)
            native_user_id = normalize_space(author.get("id")) if isinstance(author, dict) else None
            native_post_id = normalize_space(obj.get("id")) or normalize_space(obj.get("identifier")) or normalize_space(obj.get("@id"))
            key = native_post_id or sha1(f"{author_name}|{created}|{content}".encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)

            msgs.append(
                {
                    "author": author_name,
                    "user_id": native_user_id or author_name,
                    "native_user_id": native_user_id,
                    "date": normalize_space(created),
                    "date_iso": normalize_space(created),
                    "body": html_to_text(content),
                    "likes_count": sum_reactions(obj.get("reactions") or []),
                    "native_post_id": native_post_id,
                    "anchor_id": None,
                    "reply_to_post_id": normalize_space(obj.get("parent_id")) or normalize_space(obj.get("inReplyTo"))
                }
            )
        msgs = [m for m in msgs if m.get("body")]
        msgs.sort(key=lambda x: x.get("date_iso") or x.get("date") or "")
        return msgs

    def thread_messages_from_dom(self, soup: BeautifulSoup) -> List[Dict]:
        reply_nodes = []
        seen_nodes = set()
        for selector in self.selectors.get("reply_nodes") or []:
            for node in soup.select(selector):
                if id(node) in seen_nodes:
                    continue
                seen_nodes.add(id(node))
                reply_nodes.append(node)

        messages = []
        seen = set()
        for node in reply_nodes:
            body = None
            for selector in self.selectors.get("reply_body") or []:
                tag = node.select_one(selector)
                if tag:
                    body = clean_body(tag.get_text("\n", strip=True))
                    if body:
                        break
            if not body:
                body = clean_body(node.get_text("\n", strip=True))
            if not body:
                continue

            author, user_id, native_user_id = self.extract_message_user(node, self.selectors.get("reply_author") or [])
            date_text, date_iso = self.extract_message_date(node, self.selectors.get("reply_date") or [])
            native_post_id, anchor_id = self.extract_node_id(node)
            reply_to_post_id = self.extract_parent_id(node)
            likes_count = self.extract_reaction_count_from_text(node)

            key = native_post_id or anchor_id or sha1(f"{author}|{date_text}|{body}".encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)

            if not (author or date_text or native_post_id):
                continue

            messages.append(
                {
                    "author": author,
                    "user_id": user_id,
                    "native_user_id": native_user_id,
                    "date": date_text,
                    "date_iso": date_iso,
                    "body": body,
                    "likes_count": likes_count,
                    "native_post_id": native_post_id,
                    "anchor_id": anchor_id,
                    "reply_to_post_id": reply_to_post_id
                }
            )
        return messages

    def listing_opening_message(self, item: Dict) -> Dict:
        return {
            "author": item.get("thread_starter"),
            "user_id": item.get("thread_starter_id") or item.get("thread_starter"),
            "native_user_id": item.get("thread_starter_id"),
            "date": item.get("opening_post_date"),
            "date_iso": item.get("opening_post_date"),
            "body": item.get("opening_post_body"),
            "likes_count": int(item.get("likes_total_visible") or 0),
            "native_post_id": item.get("opening_post_id") or item.get("thread_id"),
            "anchor_id": None,
            "reply_to_post_id": None
        }

    def message_key(self, msg: Dict) -> str:
        return (
            normalize_space(msg.get("native_post_id"))
            or normalize_space(msg.get("anchor_id"))
            or sha1(f"{msg.get('author')}|{msg.get('date')}|{msg.get('body')}".encode("utf-8")).hexdigest()
        )

    def build_thread_record(self, item: Dict) -> Dict:
        thread_url = canonical_url(item["thread_url"])
        html = self.fetch_html(thread_url)

        thread_title = item.get("thread_title")
        category_name = item.get("category_name")
        category_slug = item.get("category_slug")
        listing_category = item.get("listing_category")
        thread_id = item.get("thread_id") or stable_thread_url_id(thread_url)

        messages = []
        if html:
            soup = BeautifulSoup(html, "lxml")
            thread_title = first_text(soup, self.selectors.get("thread_title") or []) or thread_title

            state_msgs = self.thread_messages_from_state(soup, thread_url, thread_title)
            dom_msgs = self.thread_messages_from_dom(soup)
            all_msgs = state_msgs + dom_msgs

            deduped = []
            seen = set()
            for m in all_msgs:
                key = self.message_key(m)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(m)

            deduped.sort(key=lambda x: x.get("date_iso") or x.get("date") or "")
            messages = deduped

        if not messages:
            messages = [self.listing_opening_message(item)]
        else:
            opening_key = self.message_key(self.listing_opening_message(item))
            if not messages[0].get("body"):
                messages[0] = self.listing_opening_message(item)
            if opening_key not in {self.message_key(x) for x in messages} and item.get("opening_post_body"):
                messages.insert(0, self.listing_opening_message(item))

        clean_messages = []
        seen = set()
        for m in messages:
            if not m.get("body"):
                continue
            key = self.message_key(m)
            if key in seen:
                continue
            seen.add(key)
            clean_messages.append(m)
        messages = clean_messages

        for i, m in enumerate(messages, start=1):
            msg_id = m.get("native_post_id") or m.get("anchor_id") or f"{thread_id}_{i}"
            post_url = f"{thread_url}#{m.get('anchor_id')}" if m.get("anchor_id") else thread_url
            m["thread_id"] = thread_id
            m["message_id"] = msg_id
            m["post_id"] = msg_id
            m["comment_id"] = "" if i == 1 else msg_id
            m["post_number"] = i
            m["post_url"] = post_url

        opening = messages[0]
        replies = []
        for m in messages[1:]:
            replies.append(
                {
                    "author": m.get("author"),
                    "user_id": m.get("user_id"),
                    "native_user_id": m.get("native_user_id"),
                    "date": m.get("date"),
                    "date_iso": m.get("date_iso"),
                    "body": m.get("body"),
                    "likes_count": int(m.get("likes_count") or 0),
                    "dislikes_count": 0,
                    "thread_id": thread_id,
                    "message_id": m.get("message_id"),
                    "native_post_id": m.get("native_post_id"),
                    "anchor_id": m.get("anchor_id"),
                    "post_number": m.get("post_number"),
                    "type": "comment",
                    "is_original_post": False,
                    "post_id": m.get("post_id"),
                    "comment_id": m.get("comment_id"),
                    "reply_to_post_number": "",
                    "reply_to_post_id": m.get("reply_to_post_id") or "",
                    "post_url": m.get("post_url")
                }
            )

        post = {
            "author": opening.get("author"),
            "user_id": opening.get("user_id"),
            "native_user_id": opening.get("native_user_id"),
            "date": opening.get("date"),
            "date_iso": opening.get("date_iso"),
            "body": opening.get("body"),
            "likes_count": int(opening.get("likes_count") or 0),
            "dislikes_count": 0,
            "thread_id": thread_id,
            "message_id": opening.get("message_id"),
            "native_post_id": opening.get("native_post_id"),
            "anchor_id": opening.get("anchor_id"),
            "post_number": 1,
            "type": "post",
            "is_original_post": True,
            "post_id": opening.get("post_id"),
            "comment_id": "",
            "reply_to_post_number": "",
            "reply_to_post_id": "",
            "post_url": opening.get("post_url")
        }

        likes_total = int(post["likes_count"] or 0) + sum(int(r.get("likes_count") or 0) for r in replies)
        if likes_total == 0:
            likes_total = int(item.get("likes_total_visible") or 0)
            post["likes_count"] = likes_total

        last_msg = replies[-1] if replies else post

        return {
            "source_id": self.source_id,
            "source_mode": self.source_mode,
            "thread_id": thread_id,
            "thread_url_id": item.get("thread_url_id") or stable_thread_url_id(thread_url),
            "thread_title": thread_title,
            "thread_title_detail": item.get("thread_title_detail") or thread_title,
            "thread_url": thread_url,
            "listing_category": listing_category,
            "category_id": None,
            "category_name": category_name,
            "category_slug": category_slug,
            "thread_starter": item.get("thread_starter") or post.get("author"),
            "thread_starter_id": item.get("thread_starter_id") or post.get("user_id"),
            "opening_post_id": item.get("opening_post_id") or post.get("post_id"),
            "opening_message_id": item.get("opening_message_id") or post.get("message_id"),
            "opening_post_date": item.get("opening_post_date") or post.get("date"),
            "opening_post_body": item.get("opening_post_body") or post.get("body"),
            "listing_author": item.get("listing_author") or post.get("author"),
            "listing_author_id": item.get("listing_author_id") or post.get("user_id"),
            "replies_count": item.get("replies_count") if item.get("replies_count") is not None else len(replies),
            "views_count": item.get("views_count"),
            "last_message_date": last_msg.get("date"),
            "last_message_author": last_msg.get("author"),
            "last_message_author_id": last_msg.get("user_id"),
            "last_message_id": last_msg.get("message_id"),
            "last_page": 1,
            "thread_pages_count": 1,
            "posts_count": len(messages),
            "comments_count": len(replies),
            "likes_total": likes_total,
            "post": post,
            "replies": replies
        }

    def save_thread_record(self, record: Dict) -> None:
        tid = normalize_space(record.get("thread_id"))
        if self.force_rescrape_existing:
            upsert_thread_record(self.output_file, record)
            if tid:
                self.done_thread_ids.add(tid)
            return
        jsonl_append(self.output_file, record)
        if tid:
            self.done_thread_ids.add(tid)

    def run(self) -> None:
        listing_html = self.load_listing_html()
        if not listing_html:
            return

        items = self.extract_listing_items_from_dom(listing_html)
        seen_thread_urls = set()
        page_threads = len(items)
        new_threads = 0
        skipped_existing = 0

        for item in items:
            thread_url = canonical_url(item["thread_url"])
            if thread_url in seen_thread_urls:
                continue
            seen_thread_urls.add(thread_url)

            thread_id = normalize_space(item.get("thread_id")) or stable_thread_url_id(thread_url)
            if not self.force_rescrape_existing and thread_id in self.done_thread_ids:
                skipped_existing += 1
                logging.info("skip_existing thread_id=%s thread_url=%s", thread_id, thread_url)
                continue

            record = self.build_thread_record(item)
            self.save_thread_record(record)
            new_threads += 1

            logging.info(
                "thread_saved thread_id=%s messages_comments=%s replies=%s likes_total=%s thread_url=%s",
                record.get("thread_id"),
                record.get("posts_count"),
                record.get("comments_count"),
                record.get("likes_total"),
                record.get("thread_url"),
            )

            if self.delay > 0:
                time.sleep(self.delay)

        logging.info(
            "listing_page_number=1 page_threads=%s new_threads=%s skipped_existing=%s",
            page_threads,
            new_threads,
            skipped_existing,
        )


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scrapers/scraper_request_inspire.py configs/SRC003.json")
        return 1

    config = load_json(sys.argv[1])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    scraper = InspireScraper(config)
    scraper.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
