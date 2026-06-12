import os
import re
import sys
import ast
import json
import base64
import time
import requests
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs", "SRC016")
RAW_DIR = os.path.join(OUTPUT_DIR, "api_raw")
FINAL_JSONL = os.path.join(OUTPUT_DIR, "SRC016_post_and_comment_final.jsonl")
ERROR_JSONL = os.path.join(OUTPUT_DIR, "SRC016_errors_final.jsonl")

BASE_URL = "https://www.tapatalk.com/groups/endoboard/mobiquo/mobiquo.php"

HEADERS = {
    "User-Agent": "Tapatalk/8.9.5 (Android)",
    "Accept": "*/*",
}

TARGET_FORUM_NAME = "Hysterectomy"
SLEEP_SECONDS = 1.0
MAX_RETRIES = 3


def ensure_dirs() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)


def write_jsonl(path: str, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def save_raw(name: str, text: str) -> None:
    path = os.path.join(RAW_DIR, name)
    with open(path, "w", encoding="utf-8", errors="ignore") as f:
        f.write(text)


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def decode_base64_safe(value: str) -> str:
    try:
        return base64.b64decode(value).decode("utf-8", errors="ignore")
    except Exception:
        return value


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    return str(value).strip()


def call_api(method: str, params: Dict[str, Any], save_name: Optional[str] = None) -> str:
    payload = {"method_name": method}
    payload.update(params)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(BASE_URL, data=payload, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            text = resp.text
            if save_name:
                save_raw(save_name, text)
            print(f"[API] method={method} status={resp.status_code} chars={len(text)}")
            return text
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_SECONDS * attempt)
            else:
                raise last_error
    raise last_error


def try_parse_literal(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.startswith("[") or text.startswith("{"):
        try:
            return ast.literal_eval(text)
        except Exception:
            return None
    return None


def xml_member_value(member: ET.Element) -> Any:
    value_el = member.find("value")
    if value_el is None or len(value_el) == 0:
        return value_el.text if value_el is not None else None

    child = list(value_el)[0]
    tag = child.tag.lower()

    if tag == "string":
        return child.text or ""
    if tag in {"int", "i4"}:
        return int(child.text or 0)
    if tag == "boolean":
        return str(child.text or "0")
    if tag == "base64":
        return decode_base64_safe(child.text or "")
    if tag == "array":
        return xml_array_to_list(child)
    if tag == "struct":
        return xml_struct_to_dict(child)
    return child.text or ""


def xml_struct_to_dict(struct_el: ET.Element) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for member in struct_el.findall("member"):
        name_el = member.find("name")
        if name_el is None:
            continue
        data[name_el.text or ""] = xml_member_value(member)
    return data


def xml_array_to_list(array_el: ET.Element) -> List[Any]:
    out: List[Any] = []
    data_el = array_el.find("data")
    if data_el is None:
        return out
    for value_el in data_el.findall("value"):
        if len(value_el) == 0:
            out.append(value_el.text or "")
            continue
        child = list(value_el)[0]
        tag = child.tag.lower()
        if tag == "struct":
            out.append(xml_struct_to_dict(child))
        elif tag == "array":
            out.append(xml_array_to_list(child))
        elif tag == "base64":
            out.append(decode_base64_safe(child.text or ""))
        else:
            out.append(child.text or "")
    return out


def parse_xmlrpc_response(text: str) -> Dict[str, Any]:
    root = ET.fromstring(text)
    result: Dict[str, Any] = {
        "result": None,
        "result_text": "",
        "error": "",
        "raw_struct": {},
    }

    for member in root.findall(".//member"):
        name_el = member.find("name")
        if name_el is None:
            continue
        name = name_el.text or ""
        value = xml_member_value(member)
        result["raw_struct"][name] = value

    if "result" in result["raw_struct"]:
        result["result"] = normalize_text(result["raw_struct"].get("result"))
    if "result_text" in result["raw_struct"]:
        result["result_text"] = normalize_text(result["raw_struct"].get("result_text"))
    if "error" in result["raw_struct"]:
        result["error"] = normalize_text(result["raw_struct"].get("error"))

    return result


def parse_response(text: str) -> Dict[str, Any]:
    parsed_literal = try_parse_literal(text)
    if parsed_literal is not None:
        return {
            "format": "literal",
            "ok": True,
            "data": parsed_literal,
            "result": "1",
            "result_text": "",
            "error": "",
        }

    text_strip = text.strip()
    if text_strip.startswith("<?xml") or text_strip.startswith("<methodResponse>"):
        try:
            xml_data = parse_xmlrpc_response(text_strip)
            ok = normalize_text(xml_data.get("result")) in {"1", "true", "True"}
            return {
                "format": "xmlrpc",
                "ok": ok,
                "data": xml_data.get("raw_struct", {}),
                "result": xml_data.get("result"),
                "result_text": xml_data.get("result_text", ""),
                "error": xml_data.get("error", ""),
            }
        except Exception as exc:
            return {
                "format": "xmlrpc_parse_error",
                "ok": False,
                "data": {},
                "result": "0",
                "result_text": "",
                "error": f"xml_parse_error: {exc}",
            }

    return {
        "format": "unknown",
        "ok": False,
        "data": {},
        "result": "0",
        "result_text": "",
        "error": "unknown_response_format",
    }


def extract_forums(parsed: Dict[str, Any]) -> List[dict]:
    if parsed["format"] == "literal" and isinstance(parsed["data"], list):
        return [x for x in parsed["data"] if isinstance(x, dict)]

    if parsed["format"] == "xmlrpc":
        raw = parsed.get("data", {})
        for key in ("forums", "forum_list", "list", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        nested_lists = [v for v in raw.values() if isinstance(v, list)]
        for lst in nested_lists:
            dicts = [x for x in lst if isinstance(x, dict) and ("forum_id" in x or "forum_name" in x)]
            if dicts:
                return dicts

    return []


def extract_topics(parsed: Dict[str, Any]) -> List[dict]:
    raw = parsed.get("data", {})

    if parsed["format"] == "literal" and isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict) and ("topic_id" in x or "topic_title" in x)]

    if parsed["format"] == "xmlrpc":
        for key in ("topics", "topic_list", "list", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        nested_lists = [v for v in raw.values() if isinstance(v, list)]
        for lst in nested_lists:
            dicts = [x for x in lst if isinstance(x, dict) and ("topic_id" in x or "topic_title" in x)]
            if dicts:
                return dicts

    return []


def extract_posts(parsed: Dict[str, Any]) -> List[dict]:
    raw = parsed.get("data", {})

    if parsed["format"] == "literal" and isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict) and ("post_id" in x or "post_content" in x)]

    if parsed["format"] == "xmlrpc":
        for key in ("posts", "post_list", "list", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        nested_lists = [v for v in raw.values() if isinstance(v, list)]
        for lst in nested_lists:
            dicts = [x for x in lst if isinstance(x, dict) and ("post_id" in x or "post_content" in x)]
            if dicts:
                return dicts

    return []


def get_forums() -> List[dict]:
    text = call_api("get_forum", {}, "get_forum.txt")
    parsed = parse_response(text)
    forums = extract_forums(parsed)

    print(f"[forums] parsed_format={parsed['format']} forums_found={len(forums)}")
    if not forums:
        write_jsonl(ERROR_JSONL, {
            "logged_at": now_ts(),
            "stage": "get_forum",
            "error": parsed.get("error", ""),
            "result_text": parsed.get("result_text", ""),
            "format": parsed.get("format", ""),
        })
    return forums


def find_target_forum(forums: List[dict], target_name: str) -> Optional[dict]:
    for forum in forums:
        name = normalize_text(forum.get("forum_name"))
        if name.lower() == target_name.lower():
            return forum
    for forum in forums:
        name = normalize_text(forum.get("forum_name"))
        if target_name.lower() in name.lower():
            return forum
    return None


def get_topics(forum_id: str, page: int = 1, per_page: int = 20) -> Dict[str, Any]:
    params = {
        "forum_id": forum_id,
        "start_num": (page - 1) * per_page,
        "last_num": per_page,
    }
    text = call_api("get_topic", params, f"get_topic_forum_{forum_id}_page_{page}.txt")
    parsed = parse_response(text)
    topics = extract_topics(parsed)

    print(
        f"[topics] forum_id={forum_id} page={page} "
        f"parsed_format={parsed['format']} ok={parsed['ok']} topics_found={len(topics)}"
    )

    if not parsed["ok"]:
        print(f"[topics-error] result_text={parsed.get('result_text', '')} error={parsed.get('error', '')}")

    return {
        "parsed": parsed,
        "topics": topics,
    }


def get_posts(topic_id: str, start_num: int = 0, last_num: int = 100) -> Dict[str, Any]:
    params = {
        "topic_id": topic_id,
        "start_num": start_num,
        "last_num": last_num,
    }
    text = call_api("get_thread", params, f"get_thread_topic_{topic_id}_start_{start_num}.txt")
    parsed = parse_response(text)
    posts = extract_posts(parsed)

    print(
        f"[posts] topic_id={topic_id} start={start_num} "
        f"parsed_format={parsed['format']} ok={parsed['ok']} posts_found={len(posts)}"
    )

    if not parsed["ok"]:
        print(f"[posts-error] result_text={parsed.get('result_text', '')} error={parsed.get('error', '')}")

    return {
        "parsed": parsed,
        "posts": posts,
    }


def build_thread_url(topic_id: str, title: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if slug:
        return f"https://www.tapatalk.com/groups/endoboard/{slug}-t{topic_id}.html"
    return f"https://www.tapatalk.com/groups/endoboard/-t{topic_id}.html"


def build_output_row(topic: dict, posts: List[dict], forum_name: str, forum_id: str) -> dict:
    opening = posts[0]
    replies = posts[1:]

    def g(obj: dict, *keys: str) -> str:
        for key in keys:
            if key in obj and obj[key] is not None:
                return normalize_text(obj[key])
        return ""

    thread_id = g(topic, "topic_id", "thread_id")
    title = g(topic, "topic_title", "title")
    opening_post_id = g(opening, "post_id", "message_id")
    opening_author = g(opening, "post_author_name", "author", "username")
    opening_author_id = g(opening, "post_author_id", "author_id", "user_id")
    opening_date = g(opening, "post_time", "post_date", "date")
    opening_body = g(opening, "post_content", "post_body", "body", "content")

    last = posts[-1]
    last_author = g(last, "post_author_name", "author", "username")
    last_author_id = g(last, "post_author_id", "author_id", "user_id")
    last_date = g(last, "post_time", "post_date", "date")
    last_id = g(last, "post_id", "message_id")

    opening_post = {
        "author": opening_author,
        "user_id": opening_author_id or opening_author,
        "native_user_id": opening_author_id,
        "date": opening_date,
        "date_iso": opening_date,
        "body": opening_body,
        "likes_count": 0,
        "dislikes_count": 0,
        "thread_id": thread_id,
        "message_id": opening_post_id,
        "native_post_id": opening_post_id,
        "anchor_id": "#1",
        "post_number": 1,
        "type": "post",
        "is_original_post": True,
        "post_id": opening_post_id,
        "comment_id": "",
        "reply_to_post_number": "",
        "reply_to_post_id": "",
        "post_url": build_thread_url(thread_id, title),
    }

    reply_rows = []
    for idx, post in enumerate(replies, start=2):
        pid = g(post, "post_id", "message_id")
        author = g(post, "post_author_name", "author", "username")
        author_id = g(post, "post_author_id", "author_id", "user_id")
        pdate = g(post, "post_time", "post_date", "date")
        body = g(post, "post_content", "post_body", "body", "content")

        reply_rows.append({
            "author": author,
            "user_id": author_id or author,
            "native_user_id": author_id,
            "date": pdate,
            "date_iso": pdate,
            "body": body,
            "likes_count": 0,
            "dislikes_count": 0,
            "thread_id": thread_id,
            "message_id": pid,
            "native_post_id": pid,
            "anchor_id": f"#{idx}",
            "post_number": idx,
            "type": "comment",
            "is_original_post": False,
            "post_id": pid,
            "comment_id": pid,
            "reply_to_post_number": "",
            "reply_to_post_id": "",
            "post_url": build_thread_url(thread_id, title),
        })

    return {
        "source_id": "SRC016",
        "source_mode": "tapatalk_api",
        "thread_id": thread_id,
        "thread_url_id": thread_id,
        "thread_title": title,
        "thread_title_detail": title,
        "thread_url": build_thread_url(thread_id, title),
        "listing_category": forum_name.lower().replace(" ", "-"),
        "category_id": forum_id,
        "category_name": forum_name,
        "category_slug": forum_name.lower().replace(" ", "-"),
        "thread_starter": opening_author,
        "thread_starter_id": opening_author_id or opening_author,
        "opening_post_id": opening_post_id,
        "opening_message_id": opening_post_id,
        "opening_post_date": opening_date,
        "opening_post_body": opening_body,
        "listing_author": opening_author,
        "listing_author_id": opening_author_id or opening_author,
        "replies_count": len(reply_rows),
        "views_count": None,
        "last_message_date": last_date,
        "last_message_author": last_author,
        "last_message_author_id": last_author_id or last_author,
        "last_message_id": last_id,
        "last_page": 1,
        "thread_pages_count": 1,
        "posts_count": len(posts),
        "comments_count": len(reply_rows),
        "likes_total": 0,
        "post": opening_post,
        "replies": reply_rows,
    }


def main() -> None:
    ensure_dirs()

    forums = get_forums()
    if not forums:
        print("[stop] no forums parsed from get_forum")
        return

    target = find_target_forum(forums, TARGET_FORUM_NAME)
    if not target:
        print(f"[stop] target forum not found: {TARGET_FORUM_NAME}")
        print("[forums-available]")
        for forum in forums:
            print(f"  forum_id={forum.get('forum_id')} forum_name={forum.get('forum_name')}")
        return

    forum_id = normalize_text(target.get("forum_id"))
    forum_name = normalize_text(target.get("forum_name"))
    print(f"[target] forum_id={forum_id} forum_name={forum_name}")

    page = 1
    written = 0

    while True:
        result = get_topics(forum_id=forum_id, page=page, per_page=20)
        parsed = result["parsed"]
        topics = result["topics"]

        if not parsed["ok"]:
            write_jsonl(ERROR_JSONL, {
                "logged_at": now_ts(),
                "stage": "get_topic",
                "forum_id": forum_id,
                "forum_name": forum_name,
                "page": page,
                "error": parsed.get("error", ""),
                "result_text": parsed.get("result_text", ""),
                "format": parsed.get("format", ""),
            })
            break

        if not topics:
            print(f"[done] no topics on page={page}")
            break

        for topic in topics:
            topic_id = normalize_text(topic.get("topic_id"))
            topic_title = normalize_text(topic.get("topic_title") or topic.get("title"))
            print(f"[thread] topic_id={topic_id} title={topic_title}")

            post_result = get_posts(topic_id=topic_id, start_num=0, last_num=100)
            post_parsed = post_result["parsed"]
            posts = post_result["posts"]

            if not post_parsed["ok"] or not posts:
                write_jsonl(ERROR_JSONL, {
                    "logged_at": now_ts(),
                    "stage": "get_thread",
                    "forum_id": forum_id,
                    "forum_name": forum_name,
                    "topic_id": topic_id,
                    "topic_title": topic_title,
                    "error": post_parsed.get("error", ""),
                    "result_text": post_parsed.get("result_text", ""),
                    "format": post_parsed.get("format", ""),
                })
                continue

            row = build_output_row(topic, posts, forum_name, forum_id)
            write_jsonl(FINAL_JSONL, row)
            written += 1
            print(f"[written] topic_id={topic_id} posts={len(posts)}")

            time.sleep(SLEEP_SECONDS)

        page += 1
        time.sleep(SLEEP_SECONDS)

    print(f"[finished] threads_written={written}")
    print(f"[raw_dir] {RAW_DIR}")
    print(f"[final_jsonl] {FINAL_JSONL}")
    print(f"[error_jsonl] {ERROR_JSONL}")


if __name__ == "__main__":
    main()