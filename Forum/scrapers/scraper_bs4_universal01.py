#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}\s+à\s+\d{2}h\d{2}", re.I)
DATE_LINE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}\s+à\s+\d{2}h\d{2}$", re.I)
THREAD_ID_RE = re.compile(r"sujet_(\d+)", re.I)
THREAD_PAGE_RE = re.compile(r"(.*?_)(\d+)(\.htm)$", re.I)


# =========================================================
# BASIC HELPERS
# =========================================================

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path: Path, obj: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def build_output_files(cfg: dict):
    output_dir = Path(cfg["output"]["dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%d%m%Y_%Hh%M")
    source_id = cfg["source_id"]

    posts_file = output_dir / f"{source_id}_post_and_comment_{stamp}.jsonl"
    errors_file = output_dir / f"{source_id}_errors_{stamp}.jsonl"

    return posts_file, errors_file


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

    s = str(value).replace("\xa0", " ").strip()

    if not re.fullmatch(r"\d[\d\s]*", s):
        return default

    s = s.replace(" ", "")
    return int(s) if s.isdigit() else default


def fetch_html(session: requests.Session, url: str, timeout: int) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def load_seen_thread_ids(output_dir: str, source_id: str):
    seen = set()
    output_path = Path(output_dir)

    if not output_path.exists():
        return seen

    for file_path in output_path.glob(f"{source_id}_post_and_comment_*.jsonl"):
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                thread_id = clean_text(str(obj.get("thread_id", "")))
                if thread_id:
                    seen.add(thread_id)

    return seen


# =========================================================
# URL HELPERS
# =========================================================

def build_listing_url(seed_url: str, page_num: int) -> str:
    if page_num == 1:
        return seed_url

    m = re.search(r"(.*liste_sujet-)(\d+)(\.htm)$", seed_url, re.I)
    if m:
        return f"{m.group(1)}{page_num}{m.group(3)}"

    return seed_url


def extract_thread_id(url: str) -> str:
    m = THREAD_ID_RE.search(url or "")
    return m.group(1) if m else ""


def build_thread_page_url(thread_url: str, page_num: int) -> str:
    if page_num == 1:
        return thread_url

    m = THREAD_PAGE_RE.search(thread_url)
    if m:
        return f"{m.group(1)}{page_num}{m.group(3)}"

    return thread_url


# =========================================================
# LISTING PAGE
# =========================================================

def extract_last_message_from_row(row):
    """
    Last cell often contains:
    07/04/2025 à 13:50
    Cecilebb1
    """
    last_message_date = ""
    last_message_author = ""

    tds = row.find_all("td")
    if not tds:
        return last_message_date, last_message_author

    last_td = tds[-1]
    lines = [clean_text(x) for x in last_td.get_text("\n", strip=True).splitlines() if clean_text(x)]

    for line in lines:
        if DATE_RE.search(line):
            m = DATE_RE.search(line)
            if m:
                last_message_date = m.group(0)
        else:
            if not last_message_author and line not in {"", "↩"}:
                last_message_author = line

    return last_message_date, last_message_author


def parse_listing_row(row, listing_url: str):
    """
    Parse one Doctissimo table row.
    IMPORTANT:
    keep rows even if replies/views are 0.
    """
    a = row.find("a", href=re.compile(r"sujet_\d+", re.I))
    if not a:
        return None

    href = a.get("href", "")
    full_url = urljoin(listing_url, href)
    thread_id = extract_thread_id(full_url)
    if not thread_id:
        return None

    thread_title = clean_text(a.get_text(" ", strip=True))
    if not thread_title:
        return None

    tds = row.find_all("td")

    # exact numeric cells only
    numeric_cells = []
    for td in tds:
        txt = td.get_text(" ", strip=True)
        txt = txt.replace("\xa0", " ").strip()
        if re.fullmatch(r"\d[\d\s]*", txt):
            numeric_cells.append(safe_int(txt, 0))

    last_page = 1
    replies_count = 0
    views_count = 0

    # screenshot structure usually gives:
    # [last_page, replies_count, views_count]
    # or for rows without last_page: [replies_count, views_count]
    if len(numeric_cells) >= 3:
        last_page = numeric_cells[-3]
        replies_count = numeric_cells[-2]
        views_count = numeric_cells[-1]
    elif len(numeric_cells) == 2:
        replies_count = numeric_cells[-2]
        views_count = numeric_cells[-1]
    elif len(numeric_cells) == 1:
        # very rare fallback
        replies_count = numeric_cells[0]

    listing_author = ""
    listing_author_id = ""

    # author is often in the cell before replies/views
    if len(tds) >= 4:
        possible_author = clean_text(tds[-3].get_text(" ", strip=True))
        if possible_author and not re.fullmatch(r"\d[\d\s]*", possible_author):
            listing_author = possible_author

    last_message_date, last_message_author = extract_last_message_from_row(row)

    return {
        "thread_id": thread_id,
        "thread_url_id": thread_id,
        "thread_title": thread_title,
        "thread_url": full_url,
        "last_page": last_page,
        "listing_author": listing_author,
        "listing_author_id": listing_author_id,
        "replies_count": replies_count,
        "views_count": views_count,
        "last_message_date": last_message_date,
        "last_message_author": last_message_author,
        "last_message_author_id": "",
    }


def parse_listing_page(soup: BeautifulSoup, listing_url: str):
    """
    Parse ALL rows of the Doctissimo listing table.
    This is the important fix:
    we do not rely on replies/views being > 0.
    """
    threads = []
    seen_ids = set()

    rows = soup.find_all("tr")
    for row in rows:
        thread_meta = parse_listing_row(row, listing_url)
        if not thread_meta:
            continue

        thread_id = thread_meta["thread_id"]
        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)

        threads.append(thread_meta)

    # fallback if table rows fail
    if not threads:
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "sujet_" not in href:
                continue

            full_url = urljoin(listing_url, href)
            thread_id = extract_thread_id(full_url)
            if not thread_id:
                continue

            title = clean_text(a.get_text(" ", strip=True))
            if not title:
                continue

            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)

            threads.append({
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_title": title,
                "thread_url": full_url,
                "last_page": 1,
                "listing_author": "",
                "listing_author_id": "",
                "replies_count": 0,
                "views_count": 0,
                "last_message_date": "",
                "last_message_author": "",
                "last_message_author_id": "",
            })

    return threads


# =========================================================
# THREAD PAGE
# =========================================================

def extract_thread_title(soup: BeautifulSoup) -> str:
    for sel in ["h1", "title"]:
        el = soup.select_one(sel)
        if el:
            txt = clean_text(el.get_text(" ", strip=True))
            if txt:
                return txt
    return ""


def extract_thread_pages_count(soup: BeautifulSoup, thread_url: str) -> int:
    thread_id = extract_thread_id(thread_url)
    max_page = 1

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if f"sujet_{thread_id}_" not in href:
            continue

        m = re.search(rf"sujet_{thread_id}_(\d+)\.htm", href, re.I)
        if m:
            max_page = max(max_page, int(m.group(1)))

    return max_page


def extract_author_and_user_id(block):
    author = ""
    native_user_id = ""

    bad_values = {
        "",
        "Alerter",
        "Copier le lien",
        "Le lien a été copié dans votre presse-papier",
    }

    user_btn = block.find("button", attrs={"data-id-user": True})
    if user_btn:
        native_user_id = clean_text(user_btn.get("data-id-user", ""))

        data_params = user_btn.get("data-params", "")
        if not native_user_id and data_params:
            m = re.search(r'"id_user":"?(\d+)"?', data_params)
            if m:
                native_user_id = m.group(1)

        btn_text = clean_text(user_btn.get_text(" ", strip=True))
        if btn_text and btn_text not in bad_values and len(btn_text) < 80:
            author = btn_text

    if not author:
        selectors = [
            ".md-name",
            ".username",
            ".pseudo",
            ".author",
            "[itemprop='author']",
            "a[href*='profil']",
            "a[href*='membre']",
            "button[data-id-user] + a",
            "button[data-id-user] + span",
            "button[data-id-user] + div",
            "strong",
            "b",
        ]

        for selector in selectors:
            for el in block.select(selector):
                txt = clean_text(el.get_text(" ", strip=True))
                if not txt:
                    continue
                if txt in bad_values:
                    continue
                if DATE_LINE_RE.match(txt):
                    continue
                if len(txt) > 80:
                    continue
                author = txt
                break
            if author:
                break

    if not author and user_btn:
        parent = user_btn.parent
        if parent:
            for el in parent.find_all(["a", "span", "div", "strong", "b"], recursive=True):
                txt = clean_text(el.get_text(" ", strip=True))
                if not txt:
                    continue
                if txt in bad_values:
                    continue
                if DATE_LINE_RE.match(txt):
                    continue
                if len(txt) > 80:
                    continue
                author = txt
                break

    user_id = native_user_id if native_user_id else author
    return author, user_id, native_user_id


def extract_post_date(block):
    txt = block.get_text(" ", strip=True)
    m = DATE_RE.search(txt)
    date_display = m.group(0) if m else ""

    date_iso = ""
    meta = block.find("meta", attrs={"itemprop": "dateModified"})
    if meta and meta.get("content"):
        date_iso = clean_text(meta.get("content"))

    return date_display, date_iso


def extract_post_likes(block):
    text = clean_text(block.get_text(" ", strip=True))

    likes_count = 0
    dislikes_count = 0

    like_patterns = [
        r"(\d+)\s+like[s]?\b",
        r"(\d+)\s+merci\b",
        r"(\d+)\s+utile[s]?\b",
        r"(\d+)\s+vote[s]?\b",
    ]

    dislike_patterns = [
        r"(\d+)\s+dislike[s]?\b",
        r"(\d+)\s+downvote[s]?\b",
    ]

    for pattern in like_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            likes_count = int(m.group(1))
            break

    for pattern in dislike_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            dislikes_count = int(m.group(1))
            break

    return likes_count, dislikes_count


def remove_bad_nodes_for_body(body_soup):
    bad_selectors = [
        ".md-tools",
        ".md-toolbar",
        ".toolbar",
        ".actions",
        ".signature",
        ".avatar",
        ".share",
        ".alert",
        ".report",
        ".quote_button",
        ".btn",
    ]

    for selector in bad_selectors:
        for el in body_soup.select(selector):
            el.decompose()

    for el in body_soup.find_all(["script", "style", "noscript"]):
        el.decompose()


def clean_body_lines(body: str, author: str, date_display: str) -> str:
    bad_lines = {
        "Alerter",
        "Copier le lien",
        "Le lien a été copié dans votre presse-papier",
        "Alerter Copier le lien Le lien a été copié dans votre presse-papier",
    }

    lines = [x.strip() for x in body.splitlines() if x.strip()]
    cleaned = []

    for line in lines:
        if line in bad_lines:
            continue
        cleaned.append(line)

    while cleaned:
        first = clean_text(cleaned[0])

        if author and first == clean_text(author):
            cleaned.pop(0)
            continue

        if date_display and first == clean_text(date_display):
            cleaned.pop(0)
            continue

        if DATE_LINE_RE.match(first):
            cleaned.pop(0)
            continue

        break

    return "\n".join(cleaned).strip()


def extract_post_body(block, author: str, date_display: str) -> str:
    body_soup = BeautifulSoup(str(block), "html.parser")
    remove_bad_nodes_for_body(body_soup)

    candidates = []

    for selector in [
        ".post_message",
        ".message",
        ".content",
        ".md-post-content",
        ".md-content",
        ".txt-msg",
    ]:
        for el in body_soup.select(selector):
            txt = clean_multiline_text(el.get_text("\n", strip=True))
            if txt:
                candidates.append(txt)

    if not candidates:
        for p in body_soup.find_all(["p", "blockquote", "li", "div", "span"]):
            txt = clean_multiline_text(p.get_text("\n", strip=True))
            if not txt:
                continue
            if txt in {
                "Alerter",
                "Copier le lien",
                "Le lien a été copié dans votre presse-papier",
                "Alerter Copier le lien Le lien a été copié dans votre presse-papier",
            }:
                continue
            if len(txt) < 3:
                continue
            candidates.append(txt)

    if not candidates:
        return ""

    body = max(candidates, key=len)

    body = body.replace(
        "Alerter Copier le lien Le lien a été copié dans votre presse-papier",
        ""
    )

    body = clean_body_lines(body, author, date_display)
    body = clean_multiline_text(body)

    return body


def parse_thread(session: requests.Session, thread_url: str, timeout: int, sleep_seconds: float):
    first_html = fetch_html(session, thread_url, timeout)
    first_soup = BeautifulSoup(first_html, "html.parser")

    thread_id = extract_thread_id(thread_url)
    thread_title_detail = extract_thread_title(first_soup)
    thread_pages_count = extract_thread_pages_count(first_soup, thread_url)

    all_posts = []
    seen_post_ids = set()

    for page_num in range(1, thread_pages_count + 1):
        page_url = build_thread_page_url(thread_url, page_num)

        if page_num == 1:
            soup = first_soup
        else:
            html = fetch_html(session, page_url, timeout)
            soup = BeautifulSoup(html, "html.parser")
            time.sleep(sleep_seconds)

        post_blocks = soup.select("div.md-post[data-id_post]")

        if not post_blocks:
            post_blocks = soup.select("[data-id_post]")

        if not post_blocks:
            possible_blocks = []
            for div in soup.find_all(["div", "article", "li"]):
                txt = clean_text(div.get_text(" ", strip=True))
                if not txt:
                    continue
                if DATE_RE.search(txt):
                    possible_blocks.append(div)
            post_blocks = possible_blocks

        for block in post_blocks:
            native_post_id = clean_text(block.get("data-id_post", ""))
            if not native_post_id:
                continue

            if native_post_id in seen_post_ids:
                continue
            seen_post_ids.add(native_post_id)

            author, user_id, native_user_id = extract_author_and_user_id(block)
            date_display, date_iso = extract_post_date(block)
            likes_count, dislikes_count = extract_post_likes(block)

            anchor_id = ""
            anchor_el = block.find("span", id=True)
            if anchor_el:
                anchor_id = clean_text(anchor_el.get("id", ""))

            message_id = anchor_id if anchor_id else native_post_id
            body = extract_post_body(block, author, date_display)

            if not body:
                fallback_soup = BeautifulSoup(str(block), "html.parser")
                remove_bad_nodes_for_body(fallback_soup)
                fallback_text = clean_multiline_text(fallback_soup.get_text("\n", strip=True))
                fallback_text = clean_body_lines(fallback_text, author, date_display)
                body = fallback_text

            if not body:
                continue

            all_posts.append({
                "author": author,
                "user_id": user_id,
                "native_user_id": native_user_id,
                "date": date_display,
                "date_iso": date_iso,
                "body": body,
                "likes_count": likes_count,
                "dislikes_count": dislikes_count,
                "thread_id": thread_id,
                "thread_page_number": page_num,
                "post_sequence_on_page": len(all_posts) + 1,
                "message_id": message_id,
                "native_post_id": native_post_id,
                "anchor_id": anchor_id,
            })

    return {
        "thread_id": thread_id,
        "thread_title_detail": thread_title_detail,
        "thread_pages_count": thread_pages_count,
        "posts": all_posts,
    }


# =========================================================
# MAIN SCRAPER
# =========================================================

def scrape_doctissimo(cfg: dict, session: requests.Session, posts_file: Path, errors_file: Path):
    timeout = int(cfg.get("request", {}).get("timeout_seconds", 30))
    sleep_seconds = float(cfg.get("request", {}).get("sleep_seconds", 1.0))

    start_listing_page = int(cfg.get("pagination", {}).get("start_listing_page", 1))
    end_listing_page = int(
        cfg.get("pagination", {}).get(
            "end_listing_page",
            cfg.get("pagination", {}).get("max_listing_pages", 1)
        )
    )

    source_id = cfg["source_id"]
    output_dir = cfg["output"]["dir"]

    seen_thread_ids = load_seen_thread_ids(output_dir, source_id)

    total_threads_written = 0
    total_posts_written = 0

    print(f"🚀 Starting {source_id}")
    print(f"📄 Pages: {start_listing_page} -> {end_listing_page}")
    print(f"📌 Already seen thread_ids: {len(seen_thread_ids)}")
    print("=" * 60)

    for seed_url in cfg.get("start_urls", []):
        for page_num in range(start_listing_page, end_listing_page + 1):
            listing_url = build_listing_url(seed_url, page_num)

            try:
                print(f"\n📄 Listing page {page_num}: {listing_url}")
                listing_html = fetch_html(session, listing_url, timeout)
                listing_soup = BeautifulSoup(listing_html, "html.parser")

                threads = parse_listing_page(listing_soup, listing_url)
                print(f"   ✅ Found {len(threads)} thread rows")

                page_new_threads = 0

                for i, thread_meta in enumerate(threads, start=1):
                    thread_id = thread_meta["thread_id"]
                    thread_url = thread_meta["thread_url"]

                    if thread_id in seen_thread_ids:
                        continue

                    seen_thread_ids.add(thread_id)

                    try:
                        print(
                            f"   🔗 Thread {i}/{len(threads)}: "
                            f"{thread_meta['thread_title'][:60]} | "
                            f"rep={thread_meta['replies_count']} view={thread_meta['views_count']}"
                        )

                        parsed = parse_thread(session, thread_url, timeout, sleep_seconds)
                        all_posts = parsed["posts"]

                        if not all_posts:
                            print("      ⚠️ No real posts found")
                            continue

                        opening_post = all_posts[0]
                        last_post = all_posts[-1]
                        opening_post_id = opening_post["native_post_id"]

                        for idx, post in enumerate(all_posts, start=1):
                            post["type"] = "post" if idx == 1 else "comment"
                            post["is_original_post"] = (idx == 1)
                            post["post_id"] = opening_post_id
                            post["comment_id"] = "" if idx == 1 else post["native_post_id"]

                        item = {
                            "source_id": source_id,
                            "source_mode": "forum_doctissimo",
                            "thread_id": parsed["thread_id"],
                            "thread_url_id": parsed["thread_id"],
                            "thread_title": thread_meta["thread_title"],
                            "thread_title_detail": parsed["thread_title_detail"] or thread_meta["thread_title"],
                            "thread_url": thread_url,
                            "last_page": thread_meta["last_page"] if thread_meta["last_page"] else parsed["thread_pages_count"],
                            "thread_starter": opening_post["author"],
                            "thread_starter_id": opening_post["user_id"],
                            "opening_post_id": opening_post["native_post_id"],
                            "opening_message_id": opening_post["message_id"],
                            "opening_post_date": opening_post["date"],
                            "opening_post_body": opening_post["body"],
                            "listing_author": thread_meta["listing_author"] or opening_post["author"],
                            "listing_author_id": thread_meta["listing_author_id"] or opening_post["user_id"],
                            "replies_count": thread_meta["replies_count"],
                            "views_count": thread_meta["views_count"],
                            "last_message_date": thread_meta["last_message_date"] or last_post["date"],
                            "last_message_author": thread_meta["last_message_author"] or last_post["author"],
                            "last_message_author_id": thread_meta["last_message_author_id"] or last_post["user_id"],
                            "last_message_id": last_post["message_id"],
                            "thread_pages_count": parsed["thread_pages_count"],
                            "posts_count": len(all_posts),
                            "comments_count": max(len(all_posts) - 1, 0),
                            "likes_total": sum(x.get("likes_count", 0) for x in all_posts),
                            "posts": all_posts,
                        }

                        append_jsonl(posts_file, item)

                        total_threads_written += 1
                        total_posts_written += len(all_posts)
                        page_new_threads += 1

                        print(f"      ✅ {len(all_posts)} posts written")

                    except Exception as e:
                        print(f"      ❌ Thread error: {e}")
                        append_jsonl(errors_file, {
                            "source_id": source_id,
                            "thread_id": thread_id,
                            "thread_url": thread_url,
                            "page_num": page_num,
                            "error": str(e),
                            "timestamp": datetime.now().isoformat(),
                        })

                    time.sleep(sleep_seconds)

                print(f"   📊 Page {page_num}: {page_new_threads} new threads written")

            except Exception as e:
                print(f"   ❌ Listing error: {e}")
                append_jsonl(errors_file, {
                    "source_id": source_id,
                    "listing_url": listing_url,
                    "page_num": page_num,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                })

            time.sleep(sleep_seconds)

    print("\n" + "=" * 60)
    print("🎉 COMPLETE!")
    print(f"📊 New threads scraped this run: {total_threads_written}")
    print(f"📊 New posts scraped this run: {total_posts_written}")
    print(f"📁 Output: {posts_file}")
    print(f"📁 Errors: {errors_file}")


# =========================================================
# ENTRY
# =========================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    posts_file, errors_file = build_output_files(cfg)

    session = requests.Session()
    session.headers.update(cfg.get("request", {}).get("headers", {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }))

    mode = cfg.get("mode", "")
    if mode != "forum_doctissimo":
        print(f"Mode not supported by this file: {mode}")
        return

    scrape_doctissimo(cfg, session, posts_file, errors_file)


if __name__ == "__main__":
    main()