import argparse
import json
import os
import random
import re
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://endometriosis.net/forums?page=1",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def now_iso():
    return datetime.utcnow().isoformat()


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def clean_text(value):
    if value is None:
        return ""
    value = unescape(str(value))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def html_to_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def first_int(text):
    if not text:
        return None
    txt = str(text).replace(",", "")
    m = re.search(r"(\d+)", txt)
    return int(m.group(1)) if m else None


def slugify(text):
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def parse_date_to_iso(text):
    text = clean_text(text)
    if not text:
        return ""
    for fmt in [
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
    ]:
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    return ""


def detect_anti_bot(html, final_url=""):
    text = (html or "").lower()
    markers = [
        "cf-browser-verification",
        "cloudflare",
        "captcha",
        "attention required",
        "just a moment",
        "verify you are human",
        "challenge-platform",
        "turnstile",
        "please enable javascript",
        "access denied",
    ]
    if any(marker in text for marker in markers):
        return True
    if final_url and "cdn-cgi" in final_url.lower():
        return True

    m = re.search(r"<title>(.*?)</title>", html or "", re.I | re.S)
    if m:
        title = clean_text(m.group(1)).lower()
        if title in {"just a moment...", "attention required!"}:
            return True

    return False


def build_output_paths(config):
    out_dir = config.get("output", {}).get("dir", f"outputs/{config['source_id']}")
    ensure_dir(out_dir)
    src = config["source_id"]
    return {
        "dir": out_dir,
        "data": os.path.join(out_dir, f"{src}_post_and_comment_final.jsonl"),
        "errors": os.path.join(out_dir, f"{src}_errors_final.jsonl"),
        "debug_page1": os.path.join(out_dir, "debug_page1_live.html"),
    }


def load_existing_thread_ids(path):
    existing = set()
    if not os.path.exists(path):
        return existing

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue

            thread_id = clean_text(row.get("thread_id"))
            if thread_id:
                existing.add(thread_id)

    return existing


def nuxt_extract_data(soup):
    script = soup.find("script", id="__NUXT_DATA__")
    if not script:
        return None
    raw = script.get_text(strip=False)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def nuxt_decode_value(root, value):
    if isinstance(value, dict):
        if "__ref" in value:
            ref_index = value["__ref"]
            if isinstance(ref_index, int) and 0 <= ref_index < len(root):
                return nuxt_decode_value(root, root[ref_index])
            return None
        return {k: nuxt_decode_value(root, v) for k, v in value.items()}

    if isinstance(value, list):
        if value and value[0] == "null":
            out = {}
            i = 1
            while i + 1 < len(value):
                key = value[i]
                ref = value[i + 1]
                if isinstance(ref, int) and 0 <= ref < len(root):
                    out[key] = nuxt_decode_value(root, root[ref])
                else:
                    out[key] = nuxt_decode_value(root, ref)
                i += 2
            return out
        return [nuxt_decode_value(root, item) for item in value]

    return value


def resolve_nuxt_ref(ref_value, bucket):
    if isinstance(ref_value, dict):
        return ref_value
    if isinstance(ref_value, str) and ":" in ref_value:
        return bucket.get(ref_value.split(":", 1)[1], {})
    return {}


def extract_nuxt_entities(soup):
    data = nuxt_extract_data(soup)
    if not data:
        return {"threads": {}, "replies": {}, "users": {}}

    refmap = None
    for item in data:
        if isinstance(item, dict):
            keys = [k for k in item.keys() if isinstance(k, str)]
            if any(k.startswith(("Thread:", "Reply:", "User:")) for k in keys):
                refmap = item
                break

    if not isinstance(refmap, dict):
        return {"threads": {}, "replies": {}, "users": {}}

    out = {"threads": {}, "replies": {}, "users": {}}
    for key, idx in refmap.items():
        if not isinstance(key, str) or not isinstance(idx, int):
            continue
        if idx < 0 or idx >= len(data):
            continue
        try:
            decoded = nuxt_decode_value(data, data[idx])
        except Exception:
            continue

        if key.startswith("Thread:"):
            out["threads"][key.split(":", 1)[1]] = decoded
        elif key.startswith("Reply:"):
            out["replies"][key.split(":", 1)[1]] = decoded
        elif key.startswith("User:"):
            out["users"][key.split(":", 1)[1]] = decoded

    return out


class EndoNetScraper:
    def __init__(self, config_path):
        self.config = load_json(config_path)
        self.source_id = self.config["source_id"]
        self.source_mode = self.config.get("mode", "forum_endometriosis_net")
        self.base_url = "https://endometriosis.net"
        self.paths = build_output_paths(self.config)

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

        req_cfg = self.config.get("request", {})
        self.timeout = req_cfg.get("timeout_seconds", 30)
        self.sleep_seconds = req_cfg.get("sleep_seconds", 1.5)
        self.max_retries = req_cfg.get("max_retries", 3)

        pag_cfg = self.config.get("pagination", {})
        self.start_page = pag_cfg.get("start_listing_page", 1)
        self.end_page = pag_cfg.get("end_listing_page", 1)

        self.existing_thread_ids = load_existing_thread_ids(self.paths["data"])
        self.seen_listing_urls = set()

    def log_error(self, row):
        row.setdefault("source_id", self.source_id)
        row.setdefault("timestamp", now_iso())
        append_jsonl(self.paths["errors"], row)

    def sleep(self):
        time.sleep(float(self.sleep_seconds) + random.uniform(0.2, 0.7))

    def request_html(self, url):
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                html = resp.text or ""

                if detect_anti_bot(html, resp.url):
                    return {
                        "ok": False,
                        "blocked": True,
                        "status_code": resp.status_code,
                        "html": html,
                        "final_url": resp.url,
                    }

                if resp.status_code >= 400:
                    last_error = f"http_{resp.status_code}"
                else:
                    return {
                        "ok": True,
                        "blocked": False,
                        "status_code": resp.status_code,
                        "html": html,
                        "final_url": resp.url,
                    }

            except Exception as e:
                last_error = repr(e)

            if attempt < self.max_retries:
                self.sleep()

        return {
            "ok": False,
            "blocked": False,
            "error": last_error or "request_failed",
            "final_url": url,
            "html": "",
        }

    def parse_listing_page(self, soup, page_no):
        rows = []
        seen_urls = set()

        articles = soup.select("article.thread-teaser")
        if articles:
            for article in articles:
                title_a = article.select_one("a.thread-teaser__heading-link[href]")
                if not title_a:
                    continue

                href = title_a.get("href", "").strip()
                if not href:
                    continue

                thread_url = urljoin(self.base_url, href).split("#", 1)[0]
                if thread_url in self.seen_listing_urls or thread_url in seen_urls:
                    continue

                seen_urls.add(thread_url)
                self.seen_listing_urls.add(thread_url)

                title = clean_text(title_a.get_text(" ", strip=True))

                excerpt_a = article.select_one("a.thread-teaser__excerpt-link")
                excerpt = clean_text(excerpt_a.get_text(" ", strip=True)) if excerpt_a else ""

                activity_node = article.select_one(".thread-teaser__activity")
                activity = clean_text(activity_node.get_text(" ", strip=True)) if activity_node else ""

                replies_count = 0
                for node in article.select(".engagement-bar__action"):
                    txt = clean_text(node.get_text(" ", strip=True)).lower()
                    if "repl" in txt:
                        replies_count = first_int(txt) or 0
                        break

                likes_count = 0
                reaction_btn = article.select_one(".reaction-button__count")
                if reaction_btn:
                    likes_count = first_int(reaction_btn.get_text(" ", strip=True)) or 0

                thread_url_id = urlparse(thread_url).path.rstrip("/").split("/")[-1]

                rows.append({
                    "thread_url": thread_url,
                    "thread_url_id": thread_url_id,
                    "thread_title": title,
                    "thread_title_detail": excerpt,
                    "listing_page": page_no,
                    "listing_activity": activity,
                    "replies_count": replies_count,
                    "likes_total_listing": likes_count,
                    "listing_category": "forums",
                })

            return rows

        for title_a in soup.select("a.thread-teaser__heading-link[href]"):
            href = title_a.get("href", "").strip()
            if not href:
                continue

            thread_url = urljoin(self.base_url, href).split("#", 1)[0]
            if thread_url in self.seen_listing_urls or thread_url in seen_urls:
                continue

            seen_urls.add(thread_url)
            self.seen_listing_urls.add(thread_url)

            rows.append({
                "thread_url": thread_url,
                "thread_url_id": urlparse(thread_url).path.rstrip("/").split("/")[-1],
                "thread_title": clean_text(title_a.get_text(" ", strip=True)),
                "thread_title_detail": "",
                "listing_page": page_no,
                "listing_activity": "",
                "replies_count": 0,
                "likes_total_listing": 0,
                "listing_category": "forums",
            })

        return rows

    def parse_thread_page(self, soup, listing):
        nuxt = extract_nuxt_entities(soup)
        thread_entity = next(iter(nuxt["threads"].values()), {})

        thread_id = clean_text(thread_entity.get("id")) or listing["thread_url_id"]

        title_node = soup.select_one("h1.forum-thread__heading")
        title = clean_text(title_node.get_text(" ", strip=True)) if title_node else listing["thread_title"]

        body_node = soup.select_one("div.forum-thread__body")
        opening_body = body_node.get_text("\n", strip=True) if body_node else html_to_text(thread_entity.get("body"))
        opening_body = re.sub(r"\n{3,}", "\n\n", opening_body).strip()

        header_byline = soup.select_one(".forum-thread__header .byline")
        starter_name = ""
        starter_date = ""

        if header_byline:
            name_node = header_byline.select_one(".byline__name")
            time_node = header_byline.select_one(".byline__timestamp")
            starter_name = clean_text(name_node.get_text(" ", strip=True)) if name_node else ""
            starter_date = clean_text(time_node.get_text(" ", strip=True)) if time_node else ""

        starter_date_iso = parse_date_to_iso(starter_date) or clean_text(thread_entity.get("insertedAt"))

        starter_user = resolve_nuxt_ref(thread_entity.get("user"), nuxt["users"])
        starter_user_id = clean_text(starter_user.get("id")) or starter_name

        tag_a = soup.select_one(".tag-list__tag-link[href]")
        category_name = clean_text(tag_a.get_text(" ", strip=True)) if tag_a else ""
        category_href = tag_a.get("href", "") if tag_a else ""
        category_id = None

        if category_href:
            category_id = parse_qs(urlparse(category_href).query).get("tagId", [None])[0]

        category_slug = slugify(category_name)

        opening_likes = 0
        reaction_node = soup.select_one(".forum-thread__header-bottom .reaction-button__count")
        if reaction_node:
            opening_likes = first_int(reaction_node.get_text(" ", strip=True)) or 0

        replies = []
        seen_reply_ids = set()
        reply_nodes = soup.select("#forum-thread-discussion .thread-reply")
        post_number = 2

        for node in reply_nodes:
            native_reply_id = clean_text((node.get("id") or "").replace("reply-", ""))

            if native_reply_id and native_reply_id in seen_reply_ids:
                continue
            if native_reply_id:
                seen_reply_ids.add(native_reply_id)

            byline = node.select_one(".byline")
            author = ""
            date_text = ""

            if byline:
                author_node = byline.select_one(".byline__name")
                date_node = byline.select_one(".byline__timestamp")
                author = clean_text(author_node.get_text(" ", strip=True)) if author_node else ""
                date_text = clean_text(date_node.get_text(" ", strip=True)) if date_node else ""

            date_iso = parse_date_to_iso(date_text)

            body_node = node.select_one(".thread-reply__body")
            body = body_node.get_text("\n", strip=True) if body_node else ""
            body = re.sub(r"\n{3,}", "\n\n", body).strip()

            likes_count = 0
            reaction_count = node.select_one(".reaction-button__count")
            if reaction_count:
                likes_count = first_int(reaction_count.get_text(" ", strip=True)) or 0

            reply_entity = nuxt["replies"].get(native_reply_id, {}) if native_reply_id else {}
            user_entity = resolve_nuxt_ref(reply_entity.get("user"), nuxt["users"])
            user_id = clean_text(user_entity.get("id")) or author

            if not date_iso:
                date_iso = clean_text(reply_entity.get("insertedAt"))

            reply_obj = {
                "author": author,
                "user_id": user_id,
                "native_user_id": user_id,
                "date": date_text,
                "date_iso": date_iso,
                "body": body,
                "likes_count": likes_count,
                "dislikes_count": 0,
                "thread_id": thread_id,
                "message_id": native_reply_id,
                "native_post_id": native_reply_id,
                "anchor_id": f"reply-{native_reply_id}" if native_reply_id else "",
                "post_number": post_number,
                "type": "comment",
                "is_original_post": False,
                "post_id": native_reply_id,
                "comment_id": native_reply_id,
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": f"{listing['thread_url']}#reply-{native_reply_id}" if native_reply_id else listing["thread_url"],
            }

            replies.append(reply_obj)
            post_number += 1

        opening_post_id = thread_id
        opening_message_id = thread_id

        last_message_author = starter_name
        last_message_author_id = starter_user_id
        last_message_date = starter_date_iso or starter_date
        last_message_id = opening_message_id

        if replies:
            last = replies[-1]
            last_message_author = last["author"]
            last_message_author_id = last["user_id"]
            last_message_date = last["date_iso"] or last["date"]
            last_message_id = last["message_id"]

        total_likes = opening_likes + sum(int(r.get("likes_count") or 0) for r in replies)
        visible_replies_count = len(replies)
        replies_count = max(listing.get("replies_count", 0), visible_replies_count)

        thread_pages_count = 1
        pager_links = []
        for a in soup.select('a[href*="page="]'):
            href = a.get("href", "")
            joined = urljoin(listing["thread_url"], href)
            if "/forums/" in joined or "page=" in joined:
                page_val = parse_qs(urlparse(joined).query).get("page", [None])[0]
                if page_val and str(page_val).isdigit():
                    pager_links.append(int(page_val))

        if pager_links:
            thread_pages_count = max(pager_links)

        return {
            "source_id": self.source_id,
            "source_mode": self.source_mode,
            "thread_id": thread_id,
            "thread_url_id": listing["thread_url_id"],
            "thread_title": title,
            "thread_title_detail": listing.get("thread_title_detail", ""),
            "thread_url": listing["thread_url"],
            "listing_category": listing.get("listing_category", "forums"),
            "category_id": category_id,
            "category_name": category_name,
            "category_slug": category_slug,
            "thread_starter": starter_name,
            "thread_starter_id": starter_user_id,
            "opening_post_id": opening_post_id,
            "opening_message_id": opening_message_id,
            "opening_post_date": starter_date_iso or starter_date,
            "opening_post_body": opening_body,
            "listing_author": starter_name,
            "listing_author_id": starter_user_id,
            "replies_count": replies_count,
            "views_count": None,
            "last_message_date": last_message_date,
            "last_message_author": last_message_author,
            "last_message_author_id": last_message_author_id,
            "last_message_id": last_message_id,
            "last_page": thread_pages_count,
            "thread_pages_count": thread_pages_count,
            "posts_count": 1 + len(replies),
            "comments_count": len(replies),
            "likes_total": total_likes,
            "post": {
                "author": starter_name,
                "user_id": starter_user_id,
                "native_user_id": starter_user_id,
                "date": starter_date,
                "date_iso": starter_date_iso,
                "body": opening_body,
                "likes_count": opening_likes,
                "dislikes_count": 0,
                "thread_id": thread_id,
                "message_id": opening_message_id,
                "native_post_id": opening_post_id,
                "anchor_id": "forum-thread-heading",
                "post_number": 1,
                "type": "post",
                "is_original_post": True,
                "post_id": opening_post_id,
                "comment_id": "",
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": listing["thread_url"],
            },
            "replies": replies,
        }

    def scrape(self):
        print(f"[INFO] Starting scraper for {self.source_id}")
        print(f"[INFO] Resume mode: {len(self.existing_thread_ids)} existing thread_id loaded")

        for page_no in range(self.start_page, self.end_page + 1):
            listing_url = f"{self.base_url}/forums?page={page_no}"
            print(f"[INFO] listing page number={page_no} url={listing_url}")

            resp = self.request_html(listing_url)
            if not resp.get("ok"):
                error_type = "anti_bot_blocked" if resp.get("blocked") else resp.get("error", "request_failed")
                print(f"[WARN] listing page blocked/error: {page_no} -> {error_type}")
                self.log_error({
                    "type": "listing_blocked",
                    "listing_page": page_no,
                    "listing_url": listing_url,
                    "final_url": resp.get("final_url", listing_url),
                    "error": error_type,
                })
                continue

            if page_no == 1:
                with open(self.paths["debug_page1"], "w", encoding="utf-8") as f:
                    f.write(resp["html"])
                print(f"[INFO] saved debug html: {self.paths['debug_page1']}")

            soup = BeautifulSoup(resp["html"], "html.parser")
            threads = self.parse_listing_page(soup, page_no)

            if not threads:
                self.log_error({
                    "type": "listing_empty_or_soft_block",
                    "listing_page": page_no,
                    "listing_url": listing_url,
                    "final_url": resp.get("final_url", listing_url),
                    "error": "soft_block_or_non_forum_html",
                })
                print(f"[WARN] listing page number={page_no} returned 0 threads -> soft_block_or_non_forum_html")

            page_threads = len(threads)
            new_threads = 0
            skipped_existing = 0

            print(f"[INFO] listing page number={page_no} page_threads={page_threads}")

            for listing in threads:
                if listing["thread_url_id"] in self.existing_thread_ids:
                    skipped_existing += 1
                    print(f"[INFO] skipped_existing thread_url_id={listing['thread_url_id']} url={listing['thread_url']}")
                    self.sleep()
                    continue

                thread_resp = self.request_html(listing["thread_url"])
                if not thread_resp.get("ok"):
                    error_type = "anti_bot_blocked" if thread_resp.get("blocked") else thread_resp.get("error", "request_failed")
                    print(f"[WARN] thread blocked/error: {listing['thread_url']} -> {error_type}")
                    self.log_error({
                        "type": "thread_blocked",
                        "thread_url": listing["thread_url"],
                        "thread_url_id": listing["thread_url_id"],
                        "final_url": thread_resp.get("final_url", listing["thread_url"]),
                        "error": error_type,
                    })
                    self.sleep()
                    continue

                thread_soup = BeautifulSoup(thread_resp["html"], "html.parser")
                thread_row = self.parse_thread_page(thread_soup, listing)
                thread_id = clean_text(thread_row.get("thread_id"))

                if not thread_id:
                    self.log_error({
                        "type": "thread_parse_error",
                        "thread_url": listing["thread_url"],
                        "error": "missing_thread_id",
                    })
                    self.sleep()
                    continue

                if thread_id in self.existing_thread_ids:
                    skipped_existing += 1
                    print(f"[INFO] skipped_existing thread_id={thread_id} url={listing['thread_url']}")
                    self.sleep()
                    continue

                append_jsonl(self.paths["data"], thread_row)
                self.existing_thread_ids.add(thread_id)
                new_threads += 1

                print(
                    f"[INFO] scraped thread_id={thread_id} "
                    f"messages/comments scraped={thread_row['posts_count']}/{thread_row['comments_count']}"
                )
                self.sleep()

            print(
                f"[INFO] listing page number={page_no} "
                f"page_threads={page_threads} "
                f"new_threads={new_threads} "
                f"skipped_existing={skipped_existing}"
            )
            self.sleep()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config JSON")
    args = parser.parse_args()

    scraper = EndoNetScraper(args.config)
    scraper.scrape()


if __name__ == "__main__":
    main()