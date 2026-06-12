#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

should_stop = False

JDF_THREAD_RE = re.compile(r"affich-(\d+)-", re.I)
JDF_DATE_RE = re.compile(
    r"(\d{1,2}\s+(?:janv?|févr?|fév?|fevr?|fev?|mars|avr|mai|juin|juil|août|aout|sept?|oct?|nov?|déc?|dec)\.?\s+\d{4}(?:\s+à\s+\d{1,2}[:h]\d{2})?)",
    re.I,
)

STOP_PHRASES = [
    "A voir également:",
    "Répondre",
    "Commenter",
    "Partager",
    "Afficher la suite",
    "Suivre le groupe",
    "Posez votre question",
    "LES PODCASTS DU JDF",
    "Qui sommes-nous ?",
    "Contact",
    "Publicité",
    "Recrutement",
    "Données personnelles",
    "Mentions légales",
    "Voir la dernière réponse",
    "Rechercher une discussion",
    "Créer votre discussion",
    "Participer à la discussion",
    "NEWSLETTERS",
    "Besoin d'aide ?",
    "CGV",
    "Modifié par",
    "Merci de votre réponse",
    "Réponse utile",
    "Discussions similaires",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


def signal_handler(sig, frame):
    global should_stop
    print("\n⚠️  Stopping...", flush=True)
    should_stop = True


signal.signal(signal.SIGINT, signal_handler)


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


def nz_text(value, default="") -> str:
    value = clean_text(str(value or ""))
    return value if value else default


def safe_int(value, default=0):
    if value is None:
        return default
    txt = re.sub(r"[^\d]", "", str(value))
    return int(txt) if txt else default


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_query = urlencode(query)
    normalized_path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, "", normalized_query, "")
    )


def set_query_param(url: str, param: str, value) -> str:
    parsed = urlparse(url)
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != param.lower()]
    pairs.append((param, str(value)))
    new_query = urlencode(pairs)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


def fetch_html(session: requests.Session, url: str, timeout: int, referer: str = "") -> str:
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer

    r = session.get(url, headers=headers, timeout=(10, timeout))
    r.raise_for_status()

    if not r.encoding:
        r.encoding = "utf-8"

    return r.text


def parse_jdf_date(date_str: str) -> str:
    if not date_str:
        return ""

    month_map = {
        "janv": "01", "jan": "01",
        "févr": "02", "fév": "02", "fevr": "02", "fev": "02",
        "mars": "03", "mar": "03",
        "avr": "04", "avril": "04",
        "mai": "05",
        "juin": "06",
        "juil": "07", "juillet": "07",
        "août": "08", "aout": "08",
        "sept": "09", "sep": "09",
        "oct": "10", "octo": "10",
        "nov": "11", "nove": "11",
        "déc": "12", "dec": "12", "déce": "12",
    }

    m = re.search(
        r"(\d{1,2})\s+(\w+)\.?\s+(\d{4})(?:\s+à\s+(\d{1,2})[:h](\d{2}))?",
        date_str,
        re.I,
    )
    if not m:
        return ""

    day = m.group(1).zfill(2)
    month = month_map.get(m.group(2).lower(), "01")
    year = m.group(3)
    hour = (m.group(4) or "00").zfill(2)
    minute = m.group(5) or "00"
    return f"{year}-{month}-{day}T{hour}:{minute}:00"


def clean_jdf_body(text: str) -> str:
    text = BeautifulSoup(text or "", "lxml").get_text("\n", strip=True)
    lines = [nz_text(x, "") for x in text.splitlines() if nz_text(x, "")]
    cleaned = []

    for line in lines:
        if any(phrase in line for phrase in STOP_PHRASES):
            break
        cleaned.append(line)

    return "\n".join(cleaned).strip()


def extract_thread_id(url: str) -> str:
    m = JDF_THREAD_RE.search(url or "")
    return m.group(1) if m else ""


def looks_like_forum_thread_url(url: str) -> bool:
    return "/forum/" in url and bool(JDF_THREAD_RE.search(url))


def parse_compact_int(value, default=0):
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else default


def signature_from_threads(threads):
    return tuple(t.get("thread_id", "") for t in threads if t.get("thread_id"))


def extract_jdf_listing_stats(soup: BeautifulSoup):
    text = soup.get_text(" ", strip=True)

    match = re.search(
        r"Résultats\s+([\d\s ]+)\s*-\s*([\d\s ]+)\s+sur\s+un\s+total\s+de\s+([\d\s ]+)",
        text,
        re.I,
    )
    if match:
        start_num = parse_compact_int(match.group(1), 0)
        end_num = parse_compact_int(match.group(2), 0)
        total_num = parse_compact_int(match.group(3), 0)
        per_page = end_num - start_num + 1 if start_num and end_num and end_num >= start_num else 30
        return {
            "start": start_num,
            "end": end_num,
            "total": total_num,
            "per_page": per_page or 30,
        }

    match_total = re.search(r"Résultats\s*\(([\d\s ]+)\)", text, re.I)
    if match_total:
        return {
            "start": 0,
            "end": 0,
            "total": parse_compact_int(match_total.group(1), 0),
            "per_page": 30,
        }

    return None


def detect_jdf_pagination_hint(url: str, expected_page_num: int):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    for param in ("page", "p"):
        if query.get(param) == str(expected_page_num):
            return {"type": "query", "param": param}

    return None


def build_jdf_candidate_urls(current_url: str, next_page_num: int):
    parsed = urlparse(current_url)
    base_no_query = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))

    candidates = [
        set_query_param(current_url, "page", next_page_num),
        set_query_param(current_url, "p", next_page_num),
        set_query_param(base_no_query, "page", next_page_num),
        set_query_param(base_no_query, "p", next_page_num),
    ]

    out = []
    seen = set()

    for url in candidates:
        norm = normalize_url(url)
        if norm not in seen:
            seen.add(norm)
            out.append(url)

    return out


def extract_jdf_next_href_candidates(soup: BeautifulSoup, current_url: str, next_page_num: int):
    candidates = []
    current_path = urlparse(current_url).path.rstrip("/")

    for tag in soup.find_all(["a", "button", "form"], attrs=True):
        classes = tag.get("class", [])
        if isinstance(classes, str):
            classes = [classes]

        rel_value = tag.get("rel", "")
        if isinstance(rel_value, list):
            rel_value = " ".join(rel_value)

        attrs_text = " ".join(
            [
                nz_text(tag.get_text(" ", strip=True), ""),
                nz_text(tag.get("aria-label"), ""),
                nz_text(tag.get("title"), ""),
                " ".join(classes),
                nz_text(rel_value, ""),
            ]
        ).lower()

        numeric_label_match = re.fullmatch(rf"\s*{next_page_num}\s*", attrs_text)

        if not (
            "suivant" in attrs_text
            or "next" in attrs_text
            or f"page {next_page_num}" in attrs_text
            or numeric_label_match
        ):
            continue

        for attr in ("href", "data-href", "data-url", "data-link", "action", "formaction"):
            raw = tag.get(attr)
            if not raw or raw in {"#", "javascript:void(0)", "javascript:;"}:
                continue

            abs_url = urljoin(current_url, raw)
            if urlparse(abs_url).path.rstrip("/") == current_path:
                candidates.append(abs_url)

        onclick = nz_text(tag.get("onclick"), "")
        if onclick:
            for m in re.finditer(r"""['"]([^'"]+)['"]""", onclick):
                abs_url = urljoin(current_url, m.group(1))
                if urlparse(abs_url).path.rstrip("/") == current_path:
                    candidates.append(abs_url)

    out = []
    seen = set()

    for url in candidates:
        norm = normalize_url(url)
        if norm not in seen:
            seen.add(norm)
            out.append(url)

    return out


def extract_jdf_threads_from_listing(soup: BeautifulSoup, listing_url: str, verbose: bool = True):
    threads = []
    seen_thread_ids = set()

    article_elements = soup.find_all("article", attrs={"data-id": True})

    if not article_elements:
        potential_links = soup.find_all("a", href=re.compile(r"affich-\d+", re.I))
        for link in potential_links:
            parent = link.find_parent(["article", "li", "div", "section"])
            if parent and parent not in article_elements:
                article_elements.append(parent)

    if verbose:
        print(f"   🔍 Found {len(article_elements)} potential thread containers", flush=True)

    for article in article_elements:
        link_elem = article.find("a", href=re.compile(r"affich-\d+", re.I))
        if not link_elem:
            continue

        href = link_elem.get("href", "")
        title = nz_text(link_elem.get_text(" ", strip=True), "")

        if not href or not title:
            continue

        full_url = urljoin(listing_url, href)
        match = JDF_THREAD_RE.search(full_url)
        if not match:
            continue

        thread_id = match.group(1)
        if thread_id in seen_thread_ids:
            continue
        seen_thread_ids.add(thread_id)

        container_text = article.get_text(" ", strip=True)

        replies_count = 0
        last_message_date = ""
        last_message_author = ""
        listing_author = ""

        reply_match = re.search(r"(\d+)\s*réponse", container_text, re.I)
        if reply_match:
            replies_count = int(reply_match.group(1))

        # starter often appears as "Pseudo le 2 févr."
        starter_match = re.search(r"^(.+?)\s+le\s+\d{1,2}\s+", container_text, re.I)
        if starter_match:
            listing_author = nz_text(starter_match.group(1), "")

        author_match = re.search(r"Dernière réponse(?:\s+le\s+.*?\s+)?par\s+([^\s].+)$", container_text, re.I)
        if author_match:
            last_message_author = nz_text(author_match.group(1), "")

        date_match = re.search(r"Dernière réponse\s+le\s+(.+?)\s+par", container_text, re.I)
        if date_match:
            last_message_date = nz_text(date_match.group(1), "")

        if not last_message_date:
            any_date = JDF_DATE_RE.search(container_text)
            if any_date:
                last_message_date = any_date.group(1)

        threads.append(
            {
                "thread_id": thread_id,
                "thread_url_id": thread_id,
                "thread_title": title,
                "thread_url": full_url,
                "last_page": 1,
                "listing_author": listing_author,
                "listing_author_id": listing_author,
                "replies_count": replies_count,
                "views_count": 0,
                "last_message_date": last_message_date,
                "last_message_author": last_message_author,
                "last_message_author_id": last_message_author,
            }
        )

    return threads


def extract_native_user_id_from_block(block):
    for elem in [block] + list(block.find_all(True, limit=80)):
        for attr in (
            "data-user-id",
            "data-id-user",
            "data-author-id",
            "data-profile-id",
            "data-member-id",
        ):
            value = nz_text(elem.get(attr), "")
            if value:
                return value

        href = nz_text(elem.get("href"), "")
        if href:
            match = re.search(r"/profil/(\d+)", href)
            if match:
                return match.group(1)

    return ""


def normalize_native_id(raw_value):
    raw_value = nz_text(raw_value, "")
    if not raw_value:
        return ""

    parsed = urlparse(raw_value)
    candidates = [nz_text(parsed.fragment, ""), raw_value]

    for candidate in candidates:
        if not candidate:
            continue

        if re.fullmatch(r"\d{2,}", candidate):
            return candidate

        if re.fullmatch(r"[A-Za-z0-9_-]{3,120}", candidate) and re.search(
            r"(post|message|comment|reply|answer|question)",
            candidate,
            re.I,
        ):
            return candidate

    return ""


def extract_native_post_ids_from_block(block):
    post_id = ""
    message_id = ""
    anchor_id = ""

    nodes = [block] + list(block.find_all(True, limit=100))

    for elem in nodes:
        for attr in (
            "data-post-id",
            "data-message-id",
            "data-comment-id",
            "data-answer-id",
            "data-question-id",
            "data-id-post",
            "data-id",
            "id",
            "name",
        ):
            candidate = normalize_native_id(elem.get(attr))
            if not candidate:
                continue

            if not post_id and attr in {
                "data-post-id",
                "data-comment-id",
                "data-answer-id",
                "data-question-id",
                "data-id-post",
                "data-id",
                "id",
                "name",
            }:
                post_id = candidate

            if not message_id and attr in {
                "data-message-id",
                "data-id",
                "id",
                "name",
            }:
                message_id = candidate

            if not anchor_id and attr in {"id", "name"}:
                anchor_id = candidate

        href = nz_text(elem.get("href"), "")
        if href:
            candidate = normalize_native_id(href)
            if candidate and not message_id:
                message_id = candidate
            if "#" in href and candidate and not anchor_id:
                anchor_id = candidate

    return post_id, (message_id or post_id), anchor_id


def extract_text_candidates(block):
    texts = []

    for elem in block.find_all(["p", "div", "section", "article", "blockquote", "li"]):
        txt = clean_jdf_body(elem.get_text("\n", strip=True))
        if len(txt) >= 30:
            if re.match(r"^(Réponse\s+\d+\s*/\s*\d+|Messages postés|Date d'inscription)", txt, re.I):
                continue
            texts.append((len(txt), txt))

    texts.sort(key=lambda x: x[0], reverse=True)
    return texts


def parse_jsonld_posts(soup: BeautifulSoup):
    items = []
    title = ""

    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
    for script in scripts:
        raw = (script.string or script.get_text(" ", strip=True) or "").strip()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(raw.replace("\xa0", " "))
            except Exception:
                continue

        def walk(node):
            if isinstance(node, dict):
                yield node
                for v in node.values():
                    for x in walk(v):
                        yield x
            elif isinstance(node, list):
                for sub in node:
                    for x in walk(sub):
                        yield x

        question = None
        for node in walk(data):
            node_type = node.get("@type")
            if isinstance(node_type, list):
                node_type = ",".join(node_type)
            node_type = nz_text(node_type, "")

            if node_type.lower() == "qapage" and isinstance(node.get("mainEntity"), dict):
                question = node["mainEntity"]
                break

            if node_type.lower() == "question" and question is None:
                question = node

        if not question:
            continue

        title = nz_text(question.get("name") or question.get("headline") or question.get("title"), "")
        q_author = question.get("author")
        if isinstance(q_author, dict):
            q_author = q_author.get("name", "")
        q_author = nz_text(q_author, "")

        q_text = clean_jdf_body(question.get("text") or question.get("articleBody") or "")
        q_date_iso = nz_text(question.get("dateCreated") or question.get("datePublished"), "")
        q_id = normalize_native_id(question.get("url") or question.get("@id") or "")

        items.append(
            {
                "author": q_author,
                "user_id": q_author,
                "native_user_id": "",
                "date": q_date_iso,
                "date_iso": q_date_iso,
                "body": q_text,
                "likes_count": safe_int(question.get("upvoteCount"), 0),
                "dislikes_count": 0,
                "message_id": q_id,
                "post_id": q_id,
                "comment_id": "",
                "native_post_id": q_id,
                "anchor_id": q_id,
            }
        )

        answers = []
        for key in ("acceptedAnswer", "suggestedAnswer", "answer"):
            value = question.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                answers.extend(value)
            else:
                answers.append(value)

        for ans in answers:
            if not isinstance(ans, dict):
                continue

            a_author = ans.get("author")
            if isinstance(a_author, dict):
                a_author = a_author.get("name", "")
            a_author = nz_text(a_author, "")

            a_text = clean_jdf_body(ans.get("text") or ans.get("articleBody") or "")
            a_date_iso = nz_text(ans.get("dateCreated") or ans.get("datePublished"), "")
            a_id = normalize_native_id(ans.get("url") or ans.get("@id") or "")

            items.append(
                {
                    "author": a_author,
                    "user_id": a_author,
                    "native_user_id": "",
                    "date": a_date_iso,
                    "date_iso": a_date_iso,
                    "body": a_text,
                    "likes_count": safe_int(ans.get("upvoteCount"), 0),
                    "dislikes_count": 0,
                    "message_id": a_id,
                    "post_id": a_id,
                    "comment_id": a_id,
                    "native_post_id": a_id,
                    "anchor_id": a_id,
                }
            )

        if items:
            break

    return title, items


def find_dom_blocks(soup: BeautifulSoup):
    question = None
    answers = []

    for sel in [
        '[itemprop="mainEntity"]',
        '[itemtype*="Question"]',
        '[data-question-id]',
        '[id*="question"]',
    ]:
        found = soup.select_one(sel)
        if found:
            question = found
            break

    for sel in [
        '[itemprop="suggestedAnswer"]',
        '[itemprop="acceptedAnswer"]',
        '[itemtype*="Answer"]',
        '[data-answer-id]',
        '[data-comment-id]',
        '[id*="answer"]',
        '[id*="comment"]',
        '[id*="reply"]',
    ]:
        for found in soup.select(sel):
            if question is not None and found == question:
                continue
            if found not in answers:
                answers.append(found)

    return question, answers


def parse_visible_text_fallback(soup: BeautifulSoup):
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = nz_text(h1.get_text(" ", strip=True), "")

    text = "\n".join(soup.stripped_strings)
    for marker in ["Discussions similaires", "Forum Maman", "Newsletters", "Forum dédié aux familles"]:
        text = text.split(marker)[0]

    posts = []
    after_title = text.split(title, 1)[1] if title and title in text else text
    opener_chunk = after_title.split("A voir également:", 1)[0]
    opener_chunk = opener_chunk.split("## ", 1)[0]
    opener_chunk = opener_chunk.split("Répondre (", 1)[0]

    opener_lines = [line.strip() for line in opener_chunk.splitlines() if line.strip()]
    opener_author = ""
    opener_date = ""
    opener_body_lines = []

    for line in opener_lines:
        if not opener_author and re.search(r"\s+-\s*$", line):
            opener_author = nz_text(line.rsplit("-", 1)[0], "")
            continue

        if not opener_date:
            m = JDF_DATE_RE.search(line)
            if m:
                opener_date = m.group(1)
                continue

        if "Messages postés" in line:
            continue

        opener_body_lines.append(line)

    opener_body = clean_jdf_body("\n".join(opener_body_lines))
    if opener_author or opener_body:
        posts.append(
            {
                "author": opener_author,
                "user_id": opener_author,
                "native_user_id": "",
                "date": opener_date,
                "date_iso": parse_jdf_date(opener_date),
                "body": opener_body,
                "likes_count": 0,
                "dislikes_count": 0,
                "message_id": "",
                "post_id": "",
                "comment_id": "",
                "native_post_id": "",
                "anchor_id": "",
            }
        )

    reply_parts = re.split(r"\bRéponse\s+\d+\s*/\s*\d+\b", text, flags=re.I)
    if len(reply_parts) > 1:
        for raw in reply_parts[1:]:
            chunk = raw.split("Commenter", 1)[0]
            chunk = chunk.split("Répondre", 1)[0]
            chunk = chunk.split("Discussions similaires", 1)[0]

            lines = [line.strip() for line in chunk.splitlines() if line.strip()]
            if not lines:
                continue

            header = lines[0]
            author = header.split(" Messages postés", 1)[0].strip() if "Messages postés" in header else header

            date = ""
            body_lines = []

            for line in lines[1:]:
                if not date:
                    m = JDF_DATE_RE.search(line)
                    if m:
                        date = m.group(1)
                        continue

                if "Messages postés" in line:
                    continue

                body_lines.append(line)

            body = clean_jdf_body("\n".join(body_lines))
            if not body:
                continue

            posts.append(
                {
                    "author": author,
                    "user_id": author,
                    "native_user_id": "",
                    "date": date,
                    "date_iso": parse_jdf_date(date),
                    "body": body,
                    "likes_count": 0,
                    "dislikes_count": 0,
                    "message_id": "",
                    "post_id": "",
                    "comment_id": "",
                    "native_post_id": "",
                    "anchor_id": "",
                }
            )

    return title, posts


def parse_jdf_thread(soup: BeautifulSoup, thread_id: str):
    title, items = parse_jsonld_posts(soup)
    items = [item for item in items if item.get("author") or item.get("body")]

    question_block, answer_blocks = find_dom_blocks(soup)
    dom_blocks = []
    if question_block is not None:
        dom_blocks.append(question_block)
    dom_blocks.extend(answer_blocks)

    for idx, item in enumerate(items):
        if idx >= len(dom_blocks):
            break

        block = dom_blocks[idx]
        native_user_id = extract_native_user_id_from_block(block)
        post_id, message_id, anchor_id = extract_native_post_ids_from_block(block)

        if native_user_id:
            item["native_user_id"] = native_user_id
            item["user_id"] = native_user_id
        elif item.get("author"):
            item["user_id"] = item["author"]

        if post_id:
            item["native_post_id"] = post_id
            item["post_id"] = post_id

        if message_id:
            item["message_id"] = message_id

        if anchor_id:
            item["anchor_id"] = anchor_id

        if not item.get("body"):
            text_candidates = extract_text_candidates(block)
            if text_candidates:
                item["body"] = text_candidates[0][1]

    if len(items) <= 1:
        fallback_title, fallback_posts = parse_visible_text_fallback(soup)
        if len(fallback_posts) > len(items):
            if fallback_title:
                title = fallback_title
            for idx, post in enumerate(fallback_posts):
                if idx < len(items):
                    for key in ["message_id", "post_id", "comment_id", "native_post_id", "anchor_id", "native_user_id"]:
                        if items[idx].get(key):
                            post[key] = items[idx][key]

                    if items[idx].get("user_id") and post.get("user_id") == post.get("author"):
                        post["user_id"] = items[idx]["user_id"]

                    if items[idx].get("likes_count"):
                        post["likes_count"] = items[idx]["likes_count"]

                fallback_posts[idx] = post
            items = fallback_posts

    for i, post in enumerate(items):
        post["author"] = nz_text(post.get("author"), "")
        post["user_id"] = nz_text(post.get("native_user_id") or post.get("user_id") or post.get("author"), "")
        post["message_id"] = nz_text(post.get("message_id"), "")
        post["post_id"] = nz_text(post.get("post_id"), "")
        post["comment_id"] = nz_text(post.get("comment_id"), "") if i > 0 else ""
        post["native_post_id"] = nz_text(post.get("native_post_id") or post.get("post_id"), "")
        post["anchor_id"] = nz_text(post.get("anchor_id"), "")
        post["body"] = clean_jdf_body(post.get("body", ""))

        if post.get("date") and not post.get("date_iso"):
            post["date_iso"] = parse_jdf_date(post["date"])

    return {
        "thread_title_detail": title,
        "posts": items,
    }


def resolve_jdf_next_listing_page(
    session,
    current_url,
    current_soup,
    current_threads,
    current_page_num,
    max_listing_pages,
    timeout,
    pagination_hint,
):
    page_info = extract_jdf_listing_stats(current_soup)

    has_more = False
    if page_info and page_info.get("total", 0):
        if page_info.get("end", 0) < page_info.get("total", 0):
            has_more = True
    elif current_page_num < max_listing_pages and len(current_threads) >= 30:
        has_more = True

    if not has_more:
        return None, None, None, pagination_hint

    next_page_num = current_page_num + 1
    current_sig = signature_from_threads(current_threads)
    candidates = []

    if pagination_hint and pagination_hint.get("type") == "query":
        candidates.append(set_query_param(current_url, pagination_hint["param"], next_page_num))

    candidates.extend(extract_jdf_next_href_candidates(current_soup, current_url, next_page_num))
    candidates.extend(build_jdf_candidate_urls(current_url, next_page_num))

    tested = set()

    for candidate_url in candidates:
        norm = normalize_url(candidate_url)
        if norm in tested:
            continue
        tested.add(norm)

        try:
            html = fetch_html(session, candidate_url, timeout, referer=current_url)
            soup = BeautifulSoup(html, "lxml")
            threads = extract_jdf_threads_from_listing(soup, candidate_url, verbose=False)
            next_sig = signature_from_threads(threads)

            if not next_sig:
                continue

            if next_sig == current_sig:
                continue

            detected_hint = detect_jdf_pagination_hint(candidate_url, next_page_num) or pagination_hint
            return candidate_url, soup, threads, detected_hint

        except Exception:
            continue

    return None, None, None, pagination_hint


def scrape_journaldesfemmes_forum(cfg, session, posts_file: Path, errors_file: Path):
    global should_stop

    source_id = cfg["source_id"]
    timeout = cfg.get("request", {}).get("timeout_seconds", 30)
    sleep_seconds = cfg.get("request", {}).get("sleep_seconds", 1.5)
    max_listing_pages = int(cfg.get("pagination", {}).get("max_listing_pages", 29))

    seen_thread_ids = set()
    seen_listing_signatures = set()
    seen_listing_urls = set()
    total_posts_extracted = 0
    total_threads_processed = 0
    pagination_hint = None

    print(f"🚀 Starting scraper for {source_id}", flush=True)
    print(f"📊 Max listing pages: {max_listing_pages}", flush=True)
    print(f"⏱️  Sleep: {sleep_seconds}s", flush=True)
    print("=" * 60, flush=True)

    for seed_url in cfg.get("start_urls", []):
        current_listing_url = seed_url
        preloaded_listing_soup = None
        preloaded_threads = None

        for page_num in range(1, max_listing_pages + 1):
            if should_stop:
                break

            current_norm = normalize_url(current_listing_url)
            if current_norm in seen_listing_urls:
                print(f"\n⚠️ Listing URL loop detected at page {page_num}: {current_listing_url}", flush=True)
                break
            seen_listing_urls.add(current_norm)

            try:
                print(f"\n📄 Page {page_num}/{max_listing_pages}: {current_listing_url}", flush=True)

                if preloaded_listing_soup is not None and preloaded_threads is not None:
                    listing_soup = preloaded_listing_soup
                    threads = preloaded_threads
                    preloaded_listing_soup = None
                    preloaded_threads = None
                else:
                    listing_html = fetch_html(session, current_listing_url, timeout)
                    listing_soup = BeautifulSoup(listing_html, "lxml")
                    threads = extract_jdf_threads_from_listing(listing_soup, current_listing_url, verbose=True)

                if not threads:
                    print(f"   ⚠️ No threads found on page {page_num}", flush=True)
                    break

                page_signature = signature_from_threads(threads)
                if page_signature in seen_listing_signatures:
                    print("   ⚠️ Repeated listing signature detected. Stopping to avoid looping.", flush=True)
                    break
                seen_listing_signatures.add(page_signature)

                print(f"   ✅ Found {len(threads)} thread containers on this page", flush=True)

                for thread_idx, thread_meta in enumerate(threads, start=1):
                    if should_stop:
                        break

                    thread_id = thread_meta["thread_id"]
                    thread_url = thread_meta["thread_url"]

                    if thread_id in seen_thread_ids:
                        print(f"   ⏭️  Thread {thread_idx}/{len(threads)}: ID {thread_id} already processed", flush=True)
                        continue

                    try:
                        print(f"\n   🔗 Thread {thread_idx}/{len(threads)}: {thread_meta['thread_title'][:80]}", flush=True)

                        thread_html = fetch_html(session, thread_url, timeout, referer=current_listing_url)
                        thread_soup = BeautifulSoup(thread_html, "lxml")

                        parsed = parse_jdf_thread(thread_soup, thread_id)
                        all_posts = [p for p in parsed["posts"] if p.get("author") or p.get("body")]
                        title_detail = parsed["thread_title_detail"] or thread_meta["thread_title"]

                        if not all_posts:
                            print("      ⚠️ No posts found", flush=True)
                            continue

                        for i, post in enumerate(all_posts, start=1):
                            post["type"] = "post" if i == 1 else "comment"
                            post["is_original_post"] = (i == 1)
                            post["thread_id"] = thread_id
                            post["thread_page_number"] = 1
                            post["post_sequence_on_page"] = i

                            post["user_id"] = nz_text(post.get("user_id") or post.get("author"), "")
                            post["post_id"] = nz_text(post.get("post_id") or post.get("native_post_id"), "")
                            post["message_id"] = nz_text(
                                post.get("message_id") or post.get("post_id") or post.get("anchor_id"),
                                "",
                            )

                            if i == 1:
                                post["comment_id"] = ""
                            elif not post.get("comment_id"):
                                post["comment_id"] = nz_text(post.get("post_id") or post.get("message_id"), "")

                        opening_post = all_posts[0]
                        last_post = all_posts[-1]

                        item = {
                            "source_id": source_id,
                            "source_mode": "forum_journaldesfemmes",
                            "thread_id": thread_id,
                            "thread_url_id": thread_id,
                            "thread_title": thread_meta["thread_title"],
                            "thread_title_detail": title_detail,
                            "thread_url": thread_url,
                            "last_page": 1,
                            "thread_starter": opening_post.get("author", ""),
                            "thread_starter_id": opening_post.get("user_id", ""),
                            "opening_post_id": opening_post.get("post_id", ""),
                            "opening_message_id": opening_post.get("message_id", ""),
                            "opening_post_date": opening_post.get("date", ""),
                            "opening_post_body": opening_post.get("body", ""),
                            "listing_author": thread_meta.get("listing_author") or opening_post.get("author", ""),
                            "listing_author_id": thread_meta.get("listing_author_id") or opening_post.get("user_id", ""),
                            "replies_count": max(len(all_posts) - 1, 0),
                            "views_count": thread_meta.get("views_count", 0),
                            "last_message_date": last_post.get("date", ""),
                            "last_message_author": last_post.get("author", ""),
                            "last_message_author_id": last_post.get("user_id", ""),
                            "last_message_id": last_post.get("message_id", ""),
                            "thread_pages_count": 1,
                            "posts_count": len(all_posts),
                            "comments_count": max(len(all_posts) - 1, 0),
                            "likes_total": sum(safe_int(p.get("likes_count"), 0) for p in all_posts),
                            "posts": all_posts,
                        }

                        append_jsonl(posts_file, item)

                        seen_thread_ids.add(thread_id)
                        total_threads_processed += 1
                        total_posts_extracted += len(all_posts)

                        print(f"      ✅ {len(all_posts)} posts extracted", flush=True)
                        print(
                            f"         👤 Starter: {opening_post.get('author', '')} | Last: {last_post.get('author', '')}",
                            flush=True,
                        )

                    except Exception as e:
                        print(f"      ❌ Thread error: {e}", flush=True)
                        append_jsonl(
                            errors_file,
                            {
                                "source_id": source_id,
                                "thread_id": thread_id,
                                "thread_url": thread_url,
                                "error": str(e),
                                "timestamp": datetime.now().isoformat(),
                            },
                        )

                    time.sleep(sleep_seconds)

                print(f"\n   📊 Running total: {total_threads_processed} threads, {total_posts_extracted} posts", flush=True)

                next_url, next_soup, next_threads, pagination_hint = resolve_jdf_next_listing_page(
                    session=session,
                    current_url=current_listing_url,
                    current_soup=listing_soup,
                    current_threads=threads,
                    current_page_num=page_num,
                    max_listing_pages=max_listing_pages,
                    timeout=timeout,
                    pagination_hint=pagination_hint,
                )

                if not next_url:
                    print("   ℹ️ Could not verify a next listing page. Stopping pagination.", flush=True)
                    break

                print(f"   ➡️ Verified next page: {next_url}", flush=True)

                current_listing_url = next_url
                preloaded_listing_soup = next_soup
                preloaded_threads = next_threads

                time.sleep(sleep_seconds)

            except Exception as e:
                print(f"   ❌ Listing error on page {page_num}: {e}", flush=True)
                append_jsonl(
                    errors_file,
                    {
                        "source_id": source_id,
                        "listing_url": current_listing_url,
                        "page_num": page_num,
                        "error": str(e),
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                break

        if should_stop:
            break

    print("\n" + "=" * 60, flush=True)
    print("🎉 COMPLETE!", flush=True)
    print(f"📊 Total threads: {total_threads_processed}", flush=True)
    print(f"📊 Total posts: {total_posts_extracted}", flush=True)
    print(f"📁 Output: {posts_file}", flush=True)
    print(f"📁 Errors: {errors_file}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    print(f"[BOOT] running file: {__file__}", flush=True)
    print(f"[BOOT] config: {args.config}", flush=True)

    cfg = load_config(args.config)
    posts_file, errors_file = build_output_files(cfg)

    print(f"[BOOT] output file: {posts_file}", flush=True)
    print(f"[BOOT] errors file: {errors_file}", flush=True)

    session = requests.Session()
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("request", {}).get("headers", {}))
    session.headers.update(headers)

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    mode = cfg.get("mode")
    if mode != "forum_journaldesfemmes":
        raise SystemExit(f"Unsupported mode: {mode}")

    scrape_journaldesfemmes_forum(cfg, session, posts_file, errors_file)


if __name__ == "__main__":
    main()