import os
import re
import json
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

SOURCE_ID = "SRC003"
SOURCE_MODE = "forum"

BASE_URL = "https://healthunlocked.com"
COMMUNITY_SLUG = "endometriosis-uk"
COMMUNITY_URL = f"{BASE_URL}/{COMMUNITY_SLUG}"
POSTS_URL = f"{BASE_URL}/{COMMUNITY_SLUG}/posts"

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "configs")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ----------------------------
# Generic utils
# ----------------------------

def now_tag():
    return datetime.now().strftime("%d%m%Y_%Hh%M")


def build_output_paths():
    tag = now_tag()
    posts_path = os.path.join(OUTPUT_DIR, f"{SOURCE_ID}_post_and_comment_{tag}.jsonl")
    errors_path = os.path.join(OUTPUT_DIR, f"{SOURCE_ID}_errors_{tag}.jsonl")
    return posts_path, errors_path


def write_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def clean_text(text):
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_int(text):
    if not text:
        return None
    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def safe_get(url, session, timeout=30):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


# ----------------------------
# Cookies / session
# ----------------------------

def parse_cookie_text_file(cookie_text_path):
    """Parse cookie text file in format: cookie_name : value"""
    cookies = {}
    with open(cookie_text_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                cookies[name] = value
    return cookies


def build_session_from_cookie_text(cookie_text_path):
    cookies = parse_cookie_text_file(cookie_text_path)
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": COMMUNITY_URL,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    })
    session.cookies.update(cookies)
    return session


# ----------------------------
# IDs / URL helpers
# ----------------------------

def extract_thread_id_from_url(url):
    """Extract numeric thread ID from URL like /posts/151885269/..."""
    if not url:
        return None
    m = re.search(r"/posts/(\d+)", url)
    return m.group(1) if m else None


def normalize_thread_url(href):
    if not href:
        return None
    return urljoin(BASE_URL, href.split("?")[0].split("#")[0])


def is_thread_url(url):
    return bool(url and re.search(rf"/{COMMUNITY_SLUG}/posts/\d+", url))


# ----------------------------
# Listing parsing with pagination
# ----------------------------

def get_soup_from_response(response):
    return BeautifulSoup(response.text, "lxml")


def extract_next_listing_page_url(soup, current_url):
    """Find the 'next' page link in pagination."""
    # Look for a link with rel="next" or text like "Next" / "›"
    next_link = soup.find("link", rel="next")
    if next_link and next_link.get("href"):
        return urljoin(current_url, next_link["href"])

    # Look for an anchor with class containing "next" or text "Next"
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if "next" in text or "›" in text or "»" in text:
            return urljoin(current_url, a["href"])
        if a.find("span", class_=re.compile(r"next", re.I)):
            return urljoin(current_url, a["href"])
    return None


def extract_thread_urls_from_listing(soup):
    """Extract all unique thread URLs from a listing page."""
    seen = set()
    urls = []
    for a in soup.select(f'a[href*="/{COMMUNITY_SLUG}/posts/"]'):
        href = a.get("href")
        full_url = normalize_thread_url(href)
        if not full_url or not is_thread_url(full_url):
            continue
        thread_id = extract_thread_id_from_url(full_url)
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        urls.append(full_url)
    return urls


def build_archive_urls(start_year=2000, end_year=None):
    """Generate initial archive month URLs (first page of each month)."""
    if end_year is None:
        end_year = datetime.now().year
    urls = []
    for year in range(end_year, start_year - 1, -1):
        for month in range(12, 0, -1):
            urls.append(f"{POSTS_URL}/archive/{year}/{month:02d}?page=1")
    return urls


def scrape_listing_urls(session, start_year=2000, end_year=None, sleep_sec=1.0):
    """
    Scrape all thread URLs by walking through archive pages and following pagination.
    """
    all_urls = []
    seen_ids = set()

    # Start with the main /posts page and all archive month first pages
    seed_urls = [POSTS_URL] + build_archive_urls(start_year=start_year, end_year=end_year)

    # We'll process each seed URL and follow its pagination
    for start_url in seed_urls:
        current_url = start_url
        page_num = 1
        while current_url:
            try:
                print(f"Fetching listing: {current_url}")
                r = safe_get(current_url, session)
                soup = get_soup_from_response(r)

                # Extract thread URLs from this page
                page_urls = extract_thread_urls_from_listing(soup)
                for thread_url in page_urls:
                    thread_id = extract_thread_id_from_url(thread_url)
                    if thread_id and thread_id not in seen_ids:
                        seen_ids.add(thread_id)
                        all_urls.append(thread_url)

                # Find next page URL
                next_url = extract_next_listing_page_url(soup, current_url)
                if next_url and next_url != current_url:
                    # Avoid infinite loops
                    current_url = next_url
                    page_num += 1
                    time.sleep(sleep_sec)
                else:
                    break

            except Exception as e:
                print(f"Error on listing page {current_url}: {e}")
                break

    print(f"Total unique thread URLs found: {len(all_urls)}")
    return all_urls


# ----------------------------
# Thread parsing
# ----------------------------

def parse_author_and_user(container):
    """
    Extract author, user_id, and native_user_id from a container (post or comment).
    Returns (author, user_id, native_user_id).
    """
    candidates = []
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        text = clean_text(a.get_text(" ", strip=True))
        if "/user/" in href:
            slug = href.rstrip("/").split("/user/", 1)[-1].split("/", 1)[0].strip()
            if slug:
                visible_name = text if text and text.lower() != "profile" else slug
                candidates.append({
                    "author": visible_name,
                    "user_id": visible_name,
                    "native_user_id": href
                })
    if candidates:
        return candidates[0]["author"], candidates[0]["user_id"], candidates[0]["native_user_id"]
    return None, None, None


def parse_main_body(soup):
    """Extract the real opening post body, cleaning UI elements."""
    bad_selectors = [
        "script", "style", "nav", "header", "footer", "aside", "button", "svg", "form",
        "[aria-label='toolbar']", ".toolbar", ".actions", ".reactions", ".likes",
        ".comments", ".replies", ".share", ".menu", ".dropdown"
    ]

    candidate_selectors = [
        "article", "[role='article']", "main article", "main [role='article']",
        ".post-content", ".postContent", ".post-body", ".postBody", ".body",
        ".content", ".editor-content", ".message-body", ".post-message"
    ]

    texts = []

    for selector in candidate_selectors:
        for node in soup.select(selector):
            node = BeautifulSoup(str(node), "lxml")
            for bad in bad_selectors:
                for x in node.select(bad):
                    x.decompose()
            text = clean_text(node.get_text("\n", strip=True))
            if not text:
                continue
            lowered = text.lower()
            junk_markers = [
                "copier le lien", "le lien a été copié", "alerter", "profile",
                "reply", "replies", "like", "likes", "join to keep reading",
                "sign in", "log in"
            ]
            junk_hits = sum(1 for j in junk_markers if j in lowered)
            if len(text) >= 80 and junk_hits <= 3:
                texts.append(text)

    if texts:
        texts.sort(key=len, reverse=True)
        return texts[0]

    # Fallback: paragraphs inside main/article
    fallback_blocks = []
    for container in soup.select("main, article, [role='main']"):
        parts = []
        for p in container.find_all(["p", "div"]):
            txt = clean_text(p.get_text(" ", strip=True))
            if len(txt) >= 40:
                parts.append(txt)
        joined = clean_text("\n".join(parts))
        if len(joined) >= 80:
            fallback_blocks.append(joined)
    if fallback_blocks:
        fallback_blocks.sort(key=len, reverse=True)
        return fallback_blocks[0]

    return None


def parse_title(soup):
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
    else:
        title_tag = soup.find("title")
        title = clean_text(title_tag.get_text(" ", strip=True)) if title_tag else None

    if title:
        title = re.sub(r"\s*-\s*Endometriosis UK\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*-\s*HealthUnlocked\s*$", "", title, flags=re.I)
    return title


def parse_replies_count_from_page_text(soup):
    text = clean_text(soup.get_text("\n", strip=True))
    patterns = [
        r"(\d[\d,]*)\s+Replies\b",
        r"(\d[\d,]*)\s+Reply\b",
        r"\bReplies\s+(\d[\d,]*)",
        r"\bReply\s+(\d[\d,]*)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def parse_likes_count(container):
    """Extract like count from a container (post or comment)."""
    # Look for a span/button with like count
    like_span = container.find("span", class_=re.compile(r"like|reaction", re.I))
    if like_span:
        text = like_span.get_text(strip=True)
        return extract_int(text)
    # Look for any element with like count text
    for elem in container.find_all(["span", "div", "button"], text=re.compile(r"\d+\s+like", re.I)):
        return extract_int(elem.get_text(strip=True))
    return None


def parse_date_fields(container):
    """Extract date and ISO from a container (post or comment)."""
    time_tag = container.find("time")
    if time_tag:
        raw_date = clean_text(time_tag.get_text(" ", strip=True))
        raw_iso = time_tag.get("datetime")
        return raw_date, raw_iso
    return None, None


def extract_comment_id(comment_element):
    """
    Try to get native comment ID from id attribute or data-* attribute.
    Returns string if found, else None.
    """
    # Look for id like "comment-123456"
    if comment_element.get("id"):
        m = re.search(r"comment[_-]?(\d+)", comment_element["id"], re.I)
        if m:
            return m.group(1)
    # Look for data-comment-id
    if comment_element.get("data-comment-id"):
        return str(comment_element["data-comment-id"]).strip()
    if comment_element.get("data-id"):
        return str(comment_element["data-id"]).strip()
    return None


def extract_parent_comment_id(comment_element):
    """Find parent comment ID if this is a reply (e.g., data-parent-id)."""
    if comment_element.get("data-parent-id"):
        return str(comment_element["data-parent-id"]).strip()
    # Look for a parent element with comment id in class or data
    return None


def extract_comment_rows(soup, thread_id, thread_title, thread_url):
    """
    Parse all comments on the thread page.
    Returns list of dicts, each representing one comment.
    """
    comments = []

    # Find comment containers – adjust selectors based on actual HTML
    # Common patterns: div.comment, li.comment, div[data-comment-id]
    comment_selectors = [
        "div.comment",
        "li.comment",
        "div[data-comment-id]",
        "div[id^='comment-']",
        "article.comment",
        ".comment-list > div",
        ".comments > div"
    ]

    for selector in comment_selectors:
        comment_elements = soup.select(selector)
        if comment_elements:
            break
    else:
        # If no comments found, return empty list
        return comments

    for idx, comment_elem in enumerate(comment_elements, start=1):
        try:
            # Native comment ID
            comment_id = extract_comment_id(comment_elem)
            if not comment_id:
                # Fallback: generate a temporary ID only for debugging – better to skip if no native ID?
                # We'll skip comments without native ID to avoid fake IDs.
                continue

            parent_id = extract_parent_comment_id(comment_elem)

            # Author
            author, user_id, native_user_id = parse_author_and_user(comment_elem)
            if not author:
                # If no author link, maybe it's a deleted user – assign placeholder?
                author = "[deleted]"
                user_id = "[deleted]"
                native_user_id = None

            # Date
            raw_date, raw_iso = parse_date_fields(comment_elem)

            # Body – extract text from comment body, removing UI
            body_selectors = [".comment-body", ".body", ".content", ".message"]
            body = None
            for sel in body_selectors:
                body_elem = comment_elem.select_one(sel)
                if body_elem:
                    # Remove like/reply buttons
                    for btn in body_elem.select("button, .like, .reply, .actions"):
                        btn.decompose()
                    body = clean_text(body_elem.get_text("\n", strip=True))
                    if body:
                        break
            if not body:
                # Fallback: get all text from comment element but remove author/date parts? Risky.
                # We'll skip if no clear body.
                continue

            # Likes count
            likes = parse_likes_count(comment_elem)

            comment_row = {
                "source_id": SOURCE_ID,
                "source_mode": SOURCE_MODE,

                "thread_id": thread_id,
                "article_id": None,
                "page_id": None,
                "thread_url_id": thread_id,
                "thread_title": thread_title,
                "thread_title_detail": thread_title,
                "thread_url": thread_url,
                "listing_category": COMMUNITY_SLUG,

                "replies_count": None,  # not applicable for comment
                "views_count": None,
                "likes_total": None,
                "last_message_date": None,
                "publish_date": None,
                "updated_date": None,
                "thread_pages_count": 1,

                "message_id": comment_id,
                "post_id": thread_id,
                "comment_id": comment_id,
                "native_post_id": thread_id,
                "parent_comment_id": parent_id,  # added field for nested replies

                "author": author,
                "user_id": user_id if user_id else author,
                "native_user_id": native_user_id,

                "date": raw_date,
                "date_iso": raw_iso,
                "body": body,

                "likes_count": likes,
                "dislikes_count": None,
                "page_number": 1,
                "sequence_number": idx,
                "type": "comment",
                "is_original_post": False,
            }
            comments.append(comment_row)
        except Exception as e:
            # Log error for this comment but continue
            print(f"Error parsing comment {idx} in thread {thread_id}: {e}")
            continue

    return comments


def extract_thread_row(session, thread_url):
    r = safe_get(thread_url, session)
    soup = get_soup_from_response(r)

    thread_id = extract_thread_id_from_url(thread_url)
    thread_title = parse_title(soup)
    author, user_id, native_user_id = parse_author_and_user(soup)
    raw_date, raw_date_iso = parse_date_fields(soup)
    body = parse_main_body(soup)
    replies_count = parse_replies_count_from_page_text(soup)
    likes_total = parse_likes_count(soup)  # likes on original post

    # Fallback for author if username not found
    if native_user_id and (not author or author.lower() == "profile"):
        if "/user/" in native_user_id:
            slug = native_user_id.rstrip("/").split("/user/", 1)[-1].split("/", 1)[0].strip()
            if slug:
                author = slug
                user_id = slug

    row = {
        "source_id": SOURCE_ID,
        "source_mode": SOURCE_MODE,

        "thread_id": thread_id,
        "article_id": None,
        "page_id": None,
        "thread_url_id": thread_id,
        "thread_title": thread_title,
        "thread_title_detail": thread_title,
        "thread_url": thread_url,
        "listing_category": COMMUNITY_SLUG,

        "replies_count": replies_count,
        "views_count": None,
        "likes_total": likes_total,
        "last_message_date": raw_date,
        "publish_date": raw_date,
        "updated_date": None,
        "thread_pages_count": 1,

        "message_id": thread_id,
        "post_id": thread_id,
        "comment_id": None,
        "native_post_id": thread_id,

        "author": author,
        "user_id": user_id if user_id else author,
        "native_user_id": native_user_id,

        "date": raw_date,
        "date_iso": raw_date_iso,
        "body": body,

        "likes_count": None,
        "dislikes_count": None,
        "page_number": 1,
        "sequence_number": 1,
        "type": "post",
        "is_original_post": True,
    }
    return row, soup


# ----------------------------
# Resume / save / run
# ----------------------------

def load_existing_thread_ids(jsonl_path):
    seen = set()
    if not jsonl_path or not os.path.exists(jsonl_path):
        return seen
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                thread_id = row.get("thread_id")
                if thread_id:
                    seen.add(str(thread_id))
            except Exception:
                continue
    return seen


def scrape(
    cookie_text_path,
    start_year=2000,
    end_year=None,
    sleep_sec=1.0,
    max_threads=None,
    existing_output_to_skip=None
):
    session = build_session_from_cookie_text(cookie_text_path)
    posts_out, errors_out = build_output_paths()

    seen_thread_ids = load_existing_thread_ids(existing_output_to_skip)

    print("Collecting thread URLs from listing pages...")
    thread_urls = scrape_listing_urls(
        session=session,
        start_year=start_year,
        end_year=end_year,
        sleep_sec=sleep_sec
    )

    scraped_count = 0
    for thread_url in thread_urls:
        thread_id = extract_thread_id_from_url(thread_url)
        if not thread_id:
            continue
        if thread_id in seen_thread_ids:
            continue

        try:
            print(f"Scraping thread {thread_id} ({scraped_count+1}/{len(thread_urls)})")
            main_row, soup = extract_thread_row(session, thread_url)
            write_jsonl(posts_out, main_row)

            comment_rows = extract_comment_rows(
                soup=soup,
                thread_id=main_row["thread_id"],
                thread_title=main_row["thread_title"],
                thread_url=main_row["thread_url"]
            )
            for row in comment_rows:
                write_jsonl(posts_out, row)

            seen_thread_ids.add(thread_id)
            scraped_count += 1

            if max_threads and scraped_count >= max_threads:
                break

            time.sleep(sleep_sec)

        except Exception as e:
            write_jsonl(errors_out, {
                "source_id": SOURCE_ID,
                "url": thread_url,
                "thread_id": thread_id,
                "stage": "thread_parse",
                "error": str(e),
            })
            print(f"Error on thread {thread_id}: {e}")

    print(f"Done. Posts/comments output: {posts_out}")
    print(f"Errors output: {errors_out}")


if __name__ == "__main__":
    cookie_text_path = os.path.join(CONFIG_DIR, "healthunlocked cookie.txt")

    scrape(
        cookie_text_path=cookie_text_path,
        start_year=2015,
        end_year=datetime.now().year,
        sleep_sec=1.2,
        max_threads=20,          # Set to None to scrape all 55k
        existing_output_to_skip=None  # Set to previous output file to resume
    )