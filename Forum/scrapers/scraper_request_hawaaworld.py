import json
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
UI_NOISE = {
    "عرض القائمة",
    "مشاركة",
    "إضافة رد جديد",
    "إبلاغ عن إساءة استخدام",
    "تسجيل دخول",
    "تسجيل الدخول",
    "يلزم عليك تسجيل الدخول أولًا لكتابة تعليق.",
    "الصفحة الأخيرة",
    "التالي",
    "السابق",
    "close",
    "Close",
}
RELATIVE_DATE_WORDS = (
    "منذ",
    "دقيقة",
    "دقائق",
    "ساعة",
    "ساعات",
    "يوم",
    "يومين",
    "أيام",
    "شهر",
    "شهرين",
    "أشهر",
    "سنة",
    "سنتين",
    "سنوات",
)


def normalize_digits(text: str) -> str:
    return (text or "").translate(ARABIC_DIGITS)


def normalize_space(text: Optional[str]) -> str:
    if text is None:
        return ""
    text = normalize_digits(text)
    text = text.replace("\xa0", " ").replace("\u200f", " ").replace("\u200e", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text: Optional[str]) -> str:
    return normalize_space(text)


def clean_lines_from_text(text: str) -> List[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = clean_text(raw)
        if not line:
            continue
        if line in {"​", "·", "•"}:
            continue
        lines.append(line)
    return lines


def unique_keep_order(items: List[str]) -> List[str]:
    out = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    s = clean_text(text).upper().replace(",", "").replace(" ", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)([KMB])?$", s)
    if not m:
        return None
    number = float(m.group(1))
    suffix = m.group(2)
    if suffix == "K":
        number *= 1000
    elif suffix == "M":
        number *= 1000000
    elif suffix == "B":
        number *= 1000000000
    return int(number)


def extract_metric_from_text(text: str, label: str) -> Optional[int]:
    if not text:
        return None

    for raw_line in clean_lines_from_text(text):
        line = normalize_digits(raw_line)

        m = re.search(
            rf"{re.escape(label)}\s*([0-9]+(?:\.[0-9]+)?[KMB]?)",
            line,
            flags=re.I,
        )
        if m:
            return parse_count(m.group(1))

        m = re.search(
            rf"([0-9]+(?:\.[0-9]+)?[KMB]?)\s+{re.escape(label)}",
            line,
            flags=re.I,
        )
        if m:
            return parse_count(m.group(1))
    return None


def extract_metrics_from_text(text: str) -> Dict[str, Optional[int]]:
    metrics: Dict[str, Optional[int]] = {
        "التعليقات": extract_metric_from_text(text, "التعليقات"),
        "المشاهدات": extract_metric_from_text(text, "المشاهدات"),
        "إعجاب": extract_metric_from_text(text, "إعجاب"),
        "عدم إعجاب": extract_metric_from_text(text, "عدم إعجاب"),
        "مشاركة": extract_metric_from_text(text, "مشاركة"),
    }

    lines = clean_lines_from_text(text)
    if not lines:
        return metrics

    tail = lines[-20:]
    first_labeled_idx = None
    for idx, line in enumerate(tail):
        if line.startswith("إعجاب") or line.startswith("عدم إعجاب") or line == "مشاركة":
            first_labeled_idx = idx
            break

    prefix = tail[:first_labeled_idx] if first_labeled_idx is not None else tail
    standalone_numbers: List[int] = []
    for line in prefix:
        normalized = normalize_digits(line)
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[KMB]?", normalized, flags=re.I):
            value = parse_count(normalized)
            if value is not None:
                standalone_numbers.append(value)

    if metrics["المشاهدات"] is None and standalone_numbers:
        metrics["المشاهدات"] = max(standalone_numbers)

    if metrics["التعليقات"] is None:
        if len(standalone_numbers) == 1:
            if standalone_numbers[0] == 0:
                metrics["التعليقات"] = 0
        elif len(standalone_numbers) >= 2:
            views_candidate = metrics["المشاهدات"]
            if views_candidate in standalone_numbers:
                view_idx = len(standalone_numbers) - 1 - standalone_numbers[::-1].index(views_candidate)
                if view_idx > 0:
                    metrics["التعليقات"] = standalone_numbers[view_idx - 1]
                else:
                    metrics["التعليقات"] = standalone_numbers[0]
            else:
                metrics["التعليقات"] = standalone_numbers[0]

    return metrics


def is_relative_date_text(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return False
    if any(word in t for word in RELATIVE_DATE_WORDS):
        return True
    return bool(re.search(r"\b[0-9]+\s*(دقيقة|دقائق|ساعة|ساعات|يوم|أيام|شهر|أشهر|سنة|سنوات)\b", t))


def parse_relative_date_to_iso(text: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    s = clean_text(text)
    s = s.replace("منذ ", "")

    dual_map = {
        "دقيقتين": (2, "minutes"),
        "ساعتين": (2, "hours"),
        "يومين": (2, "days"),
        "شهرين": (2, "months"),
        "سنتين": (2, "years"),
        "يوم": (1, "days"),
        "شهر": (1, "months"),
        "سنة": (1, "years"),
    }
    if s in dual_map:
        value, unit = dual_map[s]
    else:
        m = re.search(r"([0-9]+)\s*(دقيقة|دقائق|ساعة|ساعات|يوم|أيام|شهر|أشهر|سنة|سنوات)", s)
        if not m:
            return None
        value = int(m.group(1))
        unit_token = m.group(2)
        if unit_token in {"دقيقة", "دقائق"}:
            unit = "minutes"
        elif unit_token in {"ساعة", "ساعات"}:
            unit = "hours"
        elif unit_token in {"يوم", "أيام"}:
            unit = "days"
        elif unit_token in {"شهر", "أشهر"}:
            unit = "months"
        else:
            unit = "years"

    if unit == "minutes":
        dt = now - timedelta(minutes=value)
    elif unit == "hours":
        dt = now - timedelta(hours=value)
    elif unit == "days":
        dt = now - timedelta(days=value)
    elif unit == "months":
        dt = now - timedelta(days=value * 30)
    else:
        dt = now - timedelta(days=value * 365)
    return dt.isoformat()


def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: str, row: Dict) -> None:
    ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_existing_thread_ids(path: str) -> Set[str]:
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
            thread_id = str(row.get("thread_id") or "").strip()
            if thread_id:
                existing.add(thread_id)
    return existing


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    query = parsed.query
    return urlunparse((scheme, netloc, path, "", query, ""))


def absolute_url(base_url: str, href: str) -> str:
    return canonical_url(urljoin(base_url, href))


def extract_profile_slug(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/user/([^/?#]+)", url)
    return m.group(1) if m else None


def extract_thread_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = canonical_url(url)
    m = re.search(r"-(\d+)(?:/page/\d+)?$", urlparse(url).path)
    return m.group(1) if m else None


def is_thread_url(url: str) -> bool:
    path = urlparse(url).path
    if any(path.startswith(prefix) for prefix in ["/user/", "/comment/", "/search", "/tag/", "/account", "/premium", "/coupons"]):
        return False
    return bool(re.search(r"-\d+$", path.rstrip("/")))


def get_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def tag_text(tag: Tag) -> str:
    return clean_text(tag.get_text("\n", strip=True))


def parse_json_ld(soup: BeautifulSoup) -> List[dict]:
    payloads = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        data = safe_json_loads(script.get_text("\n", strip=True))
        if data is None:
            continue
        if isinstance(data, list):
            payloads.extend([x for x in data if isinstance(x, dict)])
        elif isinstance(data, dict):
            payloads.append(data)
    return payloads


class HawaaWorldScraper:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.source_id = self.config["source_id"]
        self.source_mode = self.config["source_mode"]
        self.start_url = self.config["start_url"]
        self.base_url = self.config["base_url"]
        self.output_file = self.config["output_file"]
        self.error_file = self.config["error_file"]
        self.sleep_seconds = float(self.config["request"].get("sleep_seconds", 1.0))
        self.timeout = int(self.config["request"].get("timeout", 30))
        self.max_retries = int(self.config["request"].get("max_retries", 3))
        self.backoff_seconds = float(self.config["request"].get("backoff_seconds", 2.0))
        self.max_listing_pages = int(self.config["listing"].get("max_pages", 100))
        self.max_empty_listing_pages = int(self.config["listing"].get("max_empty_pages", 2))
        self.max_thread_pages = int(self.config["thread"].get("max_pages", 500))
        self.now_utc = datetime.now(timezone.utc)

        self.session = requests.Session()
        self.session.headers.update(self.config["request"].get("headers", {}))

        ensure_dir(self.output_file)
        ensure_dir(self.error_file)

        self.existing_thread_ids = load_existing_thread_ids(self.output_file)
        self.seen_listing_thread_ids = set()

    def log_error(self, url: str, stage: str, error: str, extra: Optional[dict] = None) -> None:
        row = {
            "source_id": self.source_id,
            "url": url,
            "stage": stage,
            "error": error,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            row.update(extra)
        append_jsonl(self.error_file, row)

    def request_html(self, url: str) -> str:
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                resp.encoding = resp.encoding or "utf-8"
                return resp.text
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(f"request failed for {url}: {last_error}")

    def discover_next_listing_url(self, soup: BeautifulSoup, current_url: str, current_page: int) -> Optional[str]:
        target_page = current_page + 1
        for a in soup.find_all("a", href=True):
            text = clean_text(a.get_text(" ", strip=True))
            href = absolute_url(current_url, a["href"])
            if text == str(target_page):
                return href
            if text in {"التالي", "Next"} and "search" in href:
                return href

        parsed = urlparse(current_url)
        qs = parse_qs(parsed.query)
        candidates = []

        if "page" not in qs:
            qs_q = deepcopy(qs)
            qs_q["page"] = [str(target_page)]
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(qs_q, doseq=True), "")))
        else:
            qs_q = deepcopy(qs)
            qs_q["page"] = [str(target_page)]
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(qs_q, doseq=True), "")))

        path = parsed.path.rstrip("/")
        candidates.append(urlunparse((parsed.scheme, parsed.netloc, f"{path}/page/{target_page}", "", parsed.query, "")))

        for url in candidates:
            if canonical_url(url) != canonical_url(current_url):
                return canonical_url(url)
        return None

    def parse_listing_threads(self, soup: BeautifulSoup, page_url: str) -> List[Dict]:
        items = []
        seen_urls = set()

        for a in soup.find_all("a", href=True):
            href = absolute_url(page_url, a["href"])
            if not is_thread_url(href):
                continue
            if href in seen_urls:
                continue

            title = clean_text(a.get_text(" ", strip=True))
            if not title or len(title) < 2:
                continue

            thread_id = extract_thread_id_from_url(href)
            if not thread_id:
                continue

            container = self.find_listing_container(a)
            card_text = tag_text(container) if container else title
            same_href_texts = []
            if container:
                for aa in container.find_all("a", href=True):
                    aa_href = absolute_url(page_url, aa["href"])
                    if aa_href == href:
                        txt = clean_text(aa.get_text(" ", strip=True))
                        if txt:
                            same_href_texts.append(txt)
            same_href_texts = unique_keep_order(same_href_texts)
            title_detail = None
            for txt in same_href_texts:
                if txt != title and len(txt) > len(title):
                    title_detail = txt
                    break

            author, author_id = self.extract_author_from_listing_container(container, page_url)
            category_name, category_slug = self.extract_category_from_listing_container(container, page_url)
            replies_count = extract_metric_from_text(card_text, "التعليقات")
            views_count = extract_metric_from_text(card_text, "المشاهدات")
            likes_count = extract_metric_from_text(card_text, "إعجاب")
            last_reply_author, last_reply_date = self.extract_last_reply_from_listing_container(container)

            items.append({
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_title": title,
                "thread_title_detail": title_detail or title,
                "thread_url": href,
                "listing_author": author,
                "listing_author_id": author_id,
                "thread_starter": author,
                "thread_starter_id": author_id,
                "category_name": category_name,
                "category_slug": category_slug,
                "listing_category": category_name,
                "category_id": None,
                "replies_count": replies_count,
                "views_count": views_count,
                "likes_total_visible_from_listing": likes_count,
                "last_message_author_listing": last_reply_author,
                "last_message_date_listing": last_reply_date,
            })
            seen_urls.add(href)

        deduped = []
        seen_thread_ids = set()
        for item in items:
            if item["thread_id"] in seen_thread_ids:
                continue
            seen_thread_ids.add(item["thread_id"])
            deduped.append(item)
        return deduped

    def find_listing_container(self, anchor: Tag) -> Tag:
        for parent in [anchor] + list(anchor.parents):
            if not isinstance(parent, Tag):
                continue
            if parent.name not in {"article", "section", "div", "li", "main"}:
                continue
            text = tag_text(parent)
            if any(token in text for token in ["التعليقات", "المشاهدات", "إعجاب", "آخر رد", "إبلاغ عن إساءة استخدام"]):
                if len(text) < 5000:
                    return parent
        return anchor.parent if isinstance(anchor.parent, Tag) else anchor

    def extract_author_from_listing_container(self, container: Optional[Tag], page_url: str) -> Tuple[Optional[str], Optional[str]]:
        if not container:
            return None, None
        for a in container.find_all("a", href=True):
            href = absolute_url(page_url, a["href"])
            if "/user/" not in href:
                continue
            name = clean_text(a.get_text(" ", strip=True))
            if name and not name.lower().startswith("image"):
                return name, extract_profile_slug(href) or name
        return None, None

    def extract_category_from_listing_container(self, container: Optional[Tag], page_url: str) -> Tuple[Optional[str], Optional[str]]:
        if not container:
            return None, None
        candidates = []
        for a in container.find_all("a", href=True):
            href = absolute_url(page_url, a["href"])
            text = clean_text(a.get_text(" ", strip=True))
            if not text:
                continue
            if "/user/" in href or is_thread_url(href):
                continue
            if any(host in href for host in ["account.hawaaworld.com", "premium.hawaaworld.com", "coupons.hawaaworld.com"]):
                continue
            candidates.append((text, href))
        if not candidates:
            return None, None
        text, href = candidates[-1]
        slug = urlparse(href).path.strip("/") or None
        return text, slug

    def extract_last_reply_from_listing_container(self, container: Optional[Tag]) -> Tuple[Optional[str], Optional[str]]:
        if not container:
            return None, None
        lines = clean_lines_from_text(container.get_text("\n", strip=True))
        for i, line in enumerate(lines):
            if line == "آخر رد:":
                author = clean_text(lines[i + 1]) if i + 1 < len(lines) else None
                date = clean_text(lines[i + 2]) if i + 2 < len(lines) and is_relative_date_text(lines[i + 2]) else None
                if author and "•" in author:
                    left, _, right = author.partition("•")
                    return clean_text(left), clean_text(right)
                if author:
                    return author, date
        return None, None

    def extract_max_thread_pages(self, soup: BeautifulSoup, thread_url: str) -> int:
        thread_base = canonical_url(thread_url)
        max_page = 1
        for a in soup.find_all("a", href=True):
            href = absolute_url(thread_url, a["href"])
            if not href.startswith(thread_base):
                continue
            m = re.search(r"/page/(\d+)$", urlparse(href).path)
            if m:
                max_page = max(max_page, int(m.group(1)))
        return min(max_page, self.max_thread_pages)

    def find_opening_container(self, soup: BeautifulSoup, h1: Tag) -> Tag:
        for parent in [h1] + list(h1.parents):
            if not isinstance(parent, Tag):
                continue
            if parent.name not in {"article", "section", "div", "main"}:
                continue
            text = tag_text(parent)
            if len(text) < 100:
                continue
            if any(token in text for token in ["إبلاغ عن إساءة استخدام", "المشاهدات", "إعجاب"]):
                return parent
        return h1.parent if isinstance(h1.parent, Tag) else soup.body

    def extract_opening_author_and_date(self, container: Tag, thread_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        author = None
        author_id = None
        for a in container.find_all("a", href=True):
            href = absolute_url(thread_url, a["href"])
            if "/user/" not in href:
                continue
            text = clean_text(a.get_text(" ", strip=True))
            if text and not text.lower().startswith("image"):
                author = text
                author_id = extract_profile_slug(href) or text
                break
        date_text = None
        dt_iso = None
        dt_tag = container.find("time", attrs={"datetime": True}) or container.find(attrs={"datetime": True})
        if dt_tag and dt_tag.get("datetime"):
            dt_iso = clean_text(dt_tag.get("datetime"))
        lines = clean_lines_from_text(container.get_text("\n", strip=True))
        for line in lines[:25]:
            if is_relative_date_text(line):
                date_text = line
                if not dt_iso:
                    dt_iso = parse_relative_date_to_iso(line, self.now_utc)
                break
        return author, author_id, date_text or None

    def extract_opening_body(self, container: Tag, thread_title: str) -> str:
        lines = clean_lines_from_text(container.get_text("\n", strip=True))
        if not lines:
            return ""

        start = 0
        for i, line in enumerate(lines[:40]):
            if line == thread_title:
                start = i + 1
            if line == "إبلاغ عن إساءة استخدام":
                start = i + 1

        while start < len(lines) and (
            lines[start] in UI_NOISE
            or is_relative_date_text(lines[start])
            or lines[start] == thread_title
            or re.fullmatch(r"[0-9]+", lines[start])
        ):
            start += 1

        end = len(lines)
        for i in range(start, len(lines)):
            line = lines[i]
            if line.startswith("إعجاب ") or line.startswith("عدم إعجاب"):
                end = i
                break
            if line.endswith("المشاهدات") or line.endswith("التعليقات"):
                end = i
                break
            if line in {"يلزم عليك تسجيل الدخول أولًا لكتابة تعليق.", "خليك أول من تشارك برأيها 💁🏻‍♀️", "خليك أول من تشارك برأيها"}:
                end = i
                break
            if re.fullmatch(r"[0-9]+", line):
                lookahead = lines[i + 1 : i + 5]
                if any(x.startswith("إعجاب") or x.startswith("عدم إعجاب") or x == "مشاركة" for x in lookahead):
                    end = i
                    break

        body_lines = [ln for ln in lines[start:end] if ln not in UI_NOISE]
        return "\n".join(body_lines).strip()

    def find_reply_blocks(self, soup: BeautifulSoup, opening_container: Tag) -> List[Tag]:
        root = opening_container.parent if isinstance(opening_container.parent, Tag) else soup.body
        candidates: List[Tag] = []
        for tag in root.find_all(["article", "section", "div", "li"]):
            text = tag_text(tag)
            if not text or "إضافة رد جديد" not in text:
                continue
            if len(text) < 20 or len(text) > 8000:
                continue
            if not tag.find("a", href=re.compile(r"/user/")):
                continue
            if tag.find("h1"):
                continue
            candidates.append(tag)

        candidates.sort(key=lambda t: len(tag_text(t)))
        chosen: List[Tag] = []
        for tag in candidates:
            if any(chosen_tag in tag.find_all(True) for chosen_tag in chosen):
                continue
            chosen.append(tag)

        deduped = []
        seen_fingerprint = set()
        for tag in chosen:
            text = tag_text(tag)
            fp = text[:500]
            if fp in seen_fingerprint:
                continue
            seen_fingerprint.add(fp)
            deduped.append(tag)
        return deduped

    def extract_native_ids_from_html(self, html: str) -> Dict[str, Optional[str]]:
        patterns = {
            "comment_id": [
                r"/comment/(\d+)",
                r"comment[_-]?id[\"'\s:=]+(\d+)",
                r"data-comment-id=[\"']?(\d+)",
                r"id=[\"']comment[-_](\d+)[\"']",
                r"#comment[-_](\d+)",
            ],
            "post_id": [
                r"post[_-]?id[\"'\s:=]+(\d+)",
                r"data-post-id=[\"']?(\d+)",
                r"id=[\"']post[-_](\d+)[\"']",
                r"#post[-_](\d+)",
            ],
            "message_id": [
                r"message[_-]?id[\"'\s:=]+(\d+)",
                r"data-message-id=[\"']?(\d+)",
                r"id=[\"']message[-_](\d+)[\"']",
                r"#message[-_](\d+)",
            ],
            "anchor_id": [
                r"id=[\"']([A-Za-z_-]*(?:comment|post|message)[A-Za-z0-9_-]*)[\"']",
            ],
        }
        out = {"comment_id": None, "post_id": None, "message_id": None, "anchor_id": None}
        for key, key_patterns in patterns.items():
            for pattern in key_patterns:
                m = re.search(pattern, html, flags=re.I)
                if m:
                    out[key] = m.group(1)
                    break
        return out

    def extract_block_author(self, block: Tag, thread_url: str) -> Tuple[Optional[str], Optional[str]]:
        user_links = []
        for a in block.find_all("a", href=True):
            href = absolute_url(thread_url, a["href"])
            if "/user/" not in href:
                continue
            text = clean_text(a.get_text(" ", strip=True))
            user_links.append((text, href))

        for text, href in user_links:
            if text and not text.lower().startswith("image"):
                return text, extract_profile_slug(href) or text
        if user_links:
            text, href = user_links[0]
            return text or extract_profile_slug(href), extract_profile_slug(href) or text
        return None, None

    def extract_block_date(self, block: Tag) -> Tuple[Optional[str], Optional[str]]:
        dt_iso = None
        dt_tag = block.find("time", attrs={"datetime": True}) or block.find(attrs={"datetime": True})
        if dt_tag and dt_tag.get("datetime"):
            dt_iso = clean_text(dt_tag.get("datetime"))
        lines = clean_lines_from_text(block.get_text("\n", strip=True))
        date_text = None
        for line in lines[:20]:
            if is_relative_date_text(line):
                date_text = line
                if not dt_iso:
                    dt_iso = parse_relative_date_to_iso(line, self.now_utc)
                break
        return date_text, dt_iso

    def extract_reply_body(self, block: Tag, author: Optional[str], date_text: Optional[str]) -> str:
        lines = clean_lines_from_text(block.get_text("\n", strip=True))
        if not lines:
            return ""
        start = 0
        for i, line in enumerate(lines[:20]):
            if line == "إبلاغ عن إساءة استخدام":
                start = i + 1
            elif author and line == author:
                start = max(start, i + 1)
            elif date_text and line == date_text:
                start = max(start, i + 1)

        while start < len(lines) and (
            lines[start] in UI_NOISE
            or is_relative_date_text(lines[start])
            or (author and lines[start] == author)
            or re.fullmatch(r"[0-9]+", lines[start])
        ):
            start += 1

        end = len(lines)
        for i in range(start, len(lines)):
            line = lines[i]
            if line == "إضافة رد جديد":
                end = i
                break
            if line.startswith("إعجاب ") or line.startswith("عدم إعجاب") or line == "مشاركة":
                end = i
                break

        body_lines = [ln for ln in lines[start:end] if ln not in UI_NOISE]
        return "\n".join(body_lines).strip()

    def parse_reply_blocks(self, blocks: List[Tag], thread_meta: Dict) -> List[Dict]:
        replies = []
        seen_reply_keys = set()
        post_number = 2
        thread_views_count = thread_meta.get("views_count")
        for block in blocks:
            author, native_user_id = self.extract_block_author(block, thread_meta["thread_url"])
            date_text, date_iso = self.extract_block_date(block)
            body = self.extract_reply_body(block, author, date_text)
            if not body:
                continue

            ids = self.extract_native_ids_from_html(str(block))
            comment_id = ids["comment_id"] or ""
            native_post_id = ids["post_id"] or ids["message_id"] or comment_id or ""
            message_id = ids["message_id"] or ids["post_id"] or comment_id or ""
            anchor_id = ids["anchor_id"] or ""
            likes_count = extract_metric_from_text(tag_text(block), "إعجاب") or 0
            dislikes_count = extract_metric_from_text(tag_text(block), "عدم إعجاب") or 0
            post_url = f"{self.base_url}/comment/{comment_id}" if comment_id else thread_meta["thread_url"]
            user_id = native_user_id or author or ""

            reply_key = comment_id or f"{author}|{date_text}|{body[:200]}"
            if reply_key in seen_reply_keys:
                continue
            seen_reply_keys.add(reply_key)

            replies.append({
                "author": author or "",
                "user_id": user_id,
                "native_user_id": native_user_id or "",
                "date": date_text or "",
                "date_iso": date_iso,
                "body": body,
                "likes_count": likes_count,
                "dislikes_count": dislikes_count,
                "views_count": thread_views_count,
                "thread_id": thread_meta["thread_id"],
                "message_id": message_id,
                "native_post_id": native_post_id,
                "anchor_id": anchor_id,
                "post_number": post_number,
                "type": "comment",
                "is_original_post": False,
                "post_id": native_post_id or comment_id,
                "comment_id": comment_id,
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": post_url,
            })
            post_number += 1
        return replies

    def build_thread_record(self, listing_item: Dict) -> Dict:
        thread_url = listing_item["thread_url"]
        first_html = self.request_html(thread_url)
        first_soup = get_soup(first_html)

        h1 = first_soup.find("h1")
        if not h1:
            raise RuntimeError("missing h1 title on thread page")

        thread_title = clean_text(h1.get_text(" ", strip=True)) or listing_item["thread_title"]
        opening_container = self.find_opening_container(first_soup, h1)

        json_ld_payloads = parse_json_ld(first_soup)
        article_ld = None
        for item in json_ld_payloads:
            if item.get("@type") in {"Article", "DiscussionForumPosting", "BlogPosting"}:
                article_ld = item
                break

        author, author_id, opening_date_text = self.extract_opening_author_and_date(opening_container, thread_url)
        opening_date_iso = None
        if article_ld:
            opening_date_iso = clean_text(article_ld.get("datePublished") or article_ld.get("dateCreated") or "") or None
        if not opening_date_iso:
            opening_date_iso = parse_relative_date_to_iso(opening_date_text, self.now_utc) if opening_date_text else None

        opening_body = self.extract_opening_body(opening_container, thread_title)
        opening_metrics = extract_metrics_from_text(tag_text(opening_container))
        opening_likes = opening_metrics.get("إعجاب") or 0
        opening_dislikes = opening_metrics.get("عدم إعجاب") or 0
        replies_count_visible = listing_item.get("replies_count")
        views_count_visible = listing_item.get("views_count")
        if replies_count_visible is None:
            replies_count_visible = opening_metrics.get("التعليقات")
        if views_count_visible is None:
            views_count_visible = opening_metrics.get("المشاهدات")
        thread_pages_count = self.extract_max_thread_pages(first_soup, thread_url)
        thread_meta = dict(listing_item)
        thread_meta["views_count"] = views_count_visible

        replies: List[Dict] = []
        for page_num in range(1, thread_pages_count + 1):
            page_url = thread_url if page_num == 1 else f"{thread_url}/page/{page_num}"
            html = first_html if page_num == 1 else self.request_html(page_url)
            soup = first_soup if page_num == 1 else get_soup(html)
            blocks = self.find_reply_blocks(soup, opening_container if page_num == 1 else soup.find("body") or opening_container)
            page_replies = self.parse_reply_blocks(blocks, thread_meta)
            replies.extend(page_replies)
            print(f"  thread_id={listing_item['thread_id']} page={page_num}/{thread_pages_count} messages_comments_scraped={len(page_replies)}")
            time.sleep(self.sleep_seconds)

        final_replies = []
        seen = set()
        for reply in replies:
            key = reply.get("comment_id") or f"{reply.get('author')}|{reply.get('date')}|{reply.get('body')[:200]}"
            if key in seen:
                continue
            seen.add(key)
            final_replies.append(reply)

        thread_id = listing_item["thread_id"]
        opening_post_id = thread_id
        opening_message_id = thread_id

        last_message = final_replies[-1] if final_replies else None
        likes_total = opening_likes + sum(int(x.get("likes_count") or 0) for x in final_replies)

        record = {
            "source_id": self.source_id,
            "source_mode": self.source_mode,
            "thread_id": thread_id,
            "thread_url_id": listing_item.get("thread_url_id") or thread_id,
            "thread_title": thread_title,
            "thread_title_detail": listing_item.get("thread_title_detail") or thread_title,
            "thread_url": thread_url,
            "listing_category": listing_item.get("listing_category"),
            "category_id": None,
            "category_name": listing_item.get("category_name"),
            "category_slug": listing_item.get("category_slug"),
            "thread_starter": author or listing_item.get("thread_starter") or "",
            "thread_starter_id": author_id or listing_item.get("thread_starter_id") or (author or ""),
            "opening_post_id": opening_post_id,
            "opening_message_id": opening_message_id,
            "opening_post_date": opening_date_text or "",
            "opening_post_body": opening_body,
            "listing_author": listing_item.get("listing_author") or author or "",
            "listing_author_id": listing_item.get("listing_author_id") or author_id or (author or ""),
            "replies_count": replies_count_visible if replies_count_visible is not None else len(final_replies),
            "views_count": views_count_visible,
            "last_message_date": (last_message or {}).get("date") or listing_item.get("last_message_date_listing") or opening_date_text or "",
            "last_message_author": (last_message or {}).get("author") or listing_item.get("last_message_author_listing") or author or "",
            "last_message_author_id": (last_message or {}).get("native_user_id") or listing_item.get("last_message_author_listing") or author_id or (author or ""),
            "last_message_id": (last_message or {}).get("message_id") or opening_message_id,
            "last_page": thread_pages_count,
            "thread_pages_count": thread_pages_count,
            "posts_count": 1 + len(final_replies),
            "comments_count": len(final_replies),
            "likes_total": likes_total,
            "post": {
                "author": author or listing_item.get("thread_starter") or "",
                "user_id": author_id or author or "",
                "native_user_id": author_id or "",
                "date": opening_date_text or "",
                "date_iso": opening_date_iso,
                "body": opening_body,
                "likes_count": opening_likes,
                "dislikes_count": opening_dislikes,
                "views_count": views_count_visible,
                "thread_id": thread_id,
                "message_id": opening_message_id,
                "native_post_id": opening_post_id,
                "anchor_id": "",
                "post_number": 1,
                "type": "post",
                "is_original_post": True,
                "post_id": opening_post_id,
                "comment_id": "",
                "reply_to_post_number": "",
                "reply_to_post_id": "",
                "post_url": thread_url,
            },
            "replies": final_replies,
        }
        return record

    def scrape(self) -> None:
        page_num = 1
        current_url = self.start_url
        empty_pages = 0

        while current_url and page_num <= self.max_listing_pages:
            try:
                html = self.request_html(current_url)
                soup = get_soup(html)
                listing_items = self.parse_listing_threads(soup, current_url)
                page_threads = len(listing_items)
            except Exception as e:
                self.log_error(current_url, "listing", str(e), {"page_num": page_num})
                print(f"listing page number={page_num} failed error={e}")
                break

            new_items = []
            skipped_existing = 0
            for item in listing_items:
                thread_id = item["thread_id"]
                if thread_id in self.existing_thread_ids or thread_id in self.seen_listing_thread_ids:
                    skipped_existing += 1
                    continue
                new_items.append(item)
                self.seen_listing_thread_ids.add(thread_id)

            print(
                f"listing page number={page_num} page_threads={page_threads} new_threads={len(new_items)} skipped_existing={skipped_existing}"
            )

            if page_threads == 0:
                empty_pages += 1
            else:
                empty_pages = 0

            for item in new_items:
                try:
                    record = self.build_thread_record(item)
                    append_jsonl(self.output_file, record)
                    self.existing_thread_ids.add(record["thread_id"])
                    time.sleep(self.sleep_seconds)
                except Exception as e:
                    self.log_error(item["thread_url"], "thread", str(e), {"thread_id": item.get("thread_id")})
                    print(f"  thread_id={item.get('thread_id')} failed error={e}")

            if empty_pages >= self.max_empty_listing_pages:
                break

            next_url = self.discover_next_listing_url(soup, current_url, page_num)
            if not next_url or canonical_url(next_url) == canonical_url(current_url):
                break
            current_url = next_url
            page_num += 1
            time.sleep(self.sleep_seconds)


def main() -> None:
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = str(Path(__file__).resolve().parents[1] / "configs" / "SRC015.json")
    scraper = HawaaWorldScraper(config_path)
    scraper.scrape()


if __name__ == "__main__":
    main()
