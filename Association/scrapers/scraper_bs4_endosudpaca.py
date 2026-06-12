#!/usr/bin/env python3
"""
Scrape healthcare specialists from ENDOSUD PACA.

Usage:
  # From URL (original behavior):
  python scraper_bs4_endosudpaca.py --source-id SRC025 --output-dir outputs/SRC025
  
  # From local HTML file (using your endosudpaca.txt):
  python scraper_bs4_endosudpaca.py --input-html endosudpaca.txt --source-id SRC025 --output-dir outputs/SRC025

Output:
- outputs/SRC025/SRC025.json (full records)
- outputs/SRC025/SRC025_summary.json (statistics)
- outputs/SRC025/SRC025_errors.json (parsing errors)
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://endosudpaca.fr"
CARTO_URL = f"{BASE_URL}/lannuaire/cartographie"
LIST_URL = f"{BASE_URL}/lannuaire"
DEFAULT_SOURCE_ID = "SRC025"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Regex to match: etablissements[123] = { ... };
ASSIGNMENT_RE = re.compile(
    r"etablissements\s*\[\s*(?P<index>\d+)\s*\]\s*=\s*(?P<object>\{.*?\})\s*;",
    re.DOTALL,
)

BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
POSTAL_CITY_RE = re.compile(r"\b(?P<postal>\d{5})\s+(?P<city>[A-ZÀ-ÖØ-Ý'\-\s]+)\b")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


@dataclass
class ScrapeStats:
    source_url: str
    strategy: str
    records: int = 0
    unique_ids: int = 0
    parsing_errors: int = 0


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_html(session: requests.Session, url: str, timeout: int = 45) -> str:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def clean_address(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = unescape(raw)
    text = BR_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = text.replace("\r", "\n")
    text = text.replace("\xa0", " ")
    lines = [ln.strip(" ,;") for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return None
    if digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    return digits


def validate_float(value: Any) -> Optional[float]:
    if value in (None, "", "null"):
        return None
    try:
        num = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN check
        return None
    return num


def extract_city_postal(address: str) -> Tuple[Optional[str], Optional[str]]:
    if not address:
        return None, None
    lines = [ln.strip() for ln in address.splitlines() if ln.strip()]
    for line in reversed(lines):
        m = POSTAL_CITY_RE.search(line.upper())
        if m:
            postal = m.group("postal")
            city = m.group("city").strip().title()
            return city, postal
    return None, None


def safe_parse_js_object(obj_text: str) -> Dict[str, Any]:
    """
    Convert a JS object literal into a Python dict.
    """
    text = obj_text.strip()

    # Fast path: strict JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # Common JS -> JSON adjustments
    converted = text
    converted = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', converted)
    converted = converted.replace("\\/", "/")
    converted = re.sub(r":\s*undefined\b", ': null', converted)
    converted = re.sub(r":\s*NaN\b", ': null', converted)
    converted = re.sub(r":\s*true\b", ': true', converted)
    converted = re.sub(r":\s*false\b", ': false', converted)

    try:
        return json.loads(converted)
    except Exception:
        pass

    # Final fallback: Python literal_eval after JS null/booleans conversion
    pythonish = converted.replace("null", "None").replace("true", "True").replace("false", "False")
    return ast.literal_eval(pythonish)


def normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    address = clean_address(raw.get("adresse") or raw.get("address") or "")
    city, postal = extract_city_postal(address)
    record = {
        "id": int(raw.get("id")) if str(raw.get("id", "")).isdigit() else raw.get("id"),
        "nom": (raw.get("nom") or raw.get("name") or "").strip() or None,
        "specialite": (raw.get("specialite") or raw.get("speciality") or "").strip() or None,
        "adresse": address,
        "ville": city,
        "code_postal": postal,
        "telephone": normalize_phone(raw.get("telephone")),
        "email": (raw.get("email") or "").strip() or None,
        "typeEts": (raw.get("typeEts") or "").strip() or None,
        "referant": (raw.get("referant") or raw.get("referent") or "").strip() or None,
        "latitude": validate_float(raw.get("latitude") or raw.get("lat")),
        "longitude": validate_float(raw.get("longitude") or raw.get("lon") or raw.get("lng") or raw.get("longitude")),
    }
    return record


def incremental_write_json(path: Path, data: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_summary(path: Path, records: List[Dict[str, Any]], stats: ScrapeStats) -> None:
    specialties = Counter([r.get("specialite") for r in records if r.get("specialite")])
    cities = Counter([r.get("ville") for r in records if r.get("ville")])
    summary = {
        "source_url": stats.source_url,
        "strategy": stats.strategy,
        "records": stats.records,
        "unique_ids": stats.unique_ids,
        "parsing_errors": stats.parsing_errors,
        "top_specialites": specialties.most_common(20),
        "top_villes": cities.most_common(20),
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_cartography_js(html: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script")
    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    seen_ids = set()

    for script in scripts:
        content = script.string or script.get_text("\n", strip=False)
        if not content or "etablissements" not in content:
            continue
        for match in ASSIGNMENT_RE.finditer(content):
            index = match.group("index")
            obj_text = match.group("object")
            try:
                raw = safe_parse_js_object(obj_text)
                record = normalize_record(raw)
                rec_id = record.get("id")
                if rec_id in seen_ids:
                    continue
                seen_ids.add(rec_id)
                records.append(record)
            except Exception as exc:
                errors.append({"index": index, "error": str(exc), "snippet": obj_text[:500]})
    return records, errors


def parse_directory_cards(html: str, base_offset: int = 0) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Fallback parser for the visible directory listing pages.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    if "spécialistes correspondent" not in text.lower() and "spécialistes" not in text.lower():
        return [], []

    main = soup.find("main") or soup
    blocks = []
    current = []
    for line in main.get_text("\n", strip=True).splitlines():
        line = line.strip()
        if not line:
            continue
        # Heuristic: new card starts with all-caps-ish surname+firstname lines
        if current and re.match(r"^[A-ZÀ-ÖØ-Ý' -]{4,}[A-Za-zÀ-ÖØ-öø-ÿ' -]+$", line):
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks, start=1):
        try:
            name = block[0]
            block_text = "\n".join(block)
            specialty_parts = []
            address_parts = []
            email = None
            phone = None
            capture = None
            for line in block[1:]:
                lower = line.lower()
                if lower.startswith("spécialit"):
                    capture = "specialty"
                    continue
                if lower.startswith("coordonnées"):
                    capture = "address"
                    continue
                if EMAIL_RE.search(line):
                    email = EMAIL_RE.search(line).group(0)
                    continue
                if "téléphone" in lower:
                    m = PHONE_RE.search(line)
                    phone = normalize_phone(m.group(0) if m else line)
                    continue
                if capture == "specialty":
                    specialty_parts.append(line)
                elif capture == "address":
                    address_parts.append(line)
            address = "\n".join(address_parts).strip()
            city, postal = extract_city_postal(address)
            records.append(
                {
                    "id": None,
                    "nom": name,
                    "specialite": ", ".join(specialty_parts) if specialty_parts else None,
                    "adresse": address,
                    "ville": city,
                    "code_postal": postal,
                    "telephone": phone,
                    "email": email,
                    "typeEts": None,
                    "referant": None,
                    "latitude": None,
                    "longitude": None,
                    "source_page_offset": base_offset,
                    "fallback_block_index": idx,
                    "raw_text": block_text,
                }
            )
        except Exception as exc:
            errors.append({"block_index": idx, "error": str(exc), "block": block[:30]})
    return records, errors


def crawl_list_fallback(session: requests.Session, max_pages: int = 40, page_step: int = 20) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    all_records: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []
    seen_signatures = set()

    for page_num in range(max_pages):
        offset = page_num * page_step
        url = LIST_URL if offset == 0 else f"{LIST_URL}?start={offset}"
        html = fetch_html(session, url)
        records, errors = parse_directory_cards(html, base_offset=offset)
        all_errors.extend(errors)
        if not records:
            break
        new_count = 0
        for rec in records:
            signature = (rec.get("nom"), rec.get("adresse"), rec.get("specialite"))
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            all_records.append(rec)
            new_count += 1
        if new_count == 0:
            break
    return all_records, all_errors


def scrape(source_id: str, output_dir: Path, input_html: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], ScrapeStats]:
    """
    If input_html is provided, read from that file instead of fetching from URL.
    """
    errors: List[Dict[str, Any]] = []

    if input_html:
        # Read from local file
        html_path = Path(input_html)
        if not html_path.exists():
            raise FileNotFoundError(f"Input HTML file not found: {input_html}")
        html = html_path.read_text(encoding="utf-8")
        source_url = str(html_path.absolute())
    else:
        # Fetch from URL
        session = build_session()
        html = fetch_html(session, CARTO_URL)
        source_url = CARTO_URL

    records, js_errors = parse_cartography_js(html)
    errors.extend(js_errors)

    strategy = "cartography_js"
    if not records and not input_html:
        # Only do fallback if we're in URL mode
        session = build_session()
        records, fallback_errors = crawl_list_fallback(session)
        errors.extend(fallback_errors)
        strategy = "directory_fallback"

    # Deduplicate
    deduped = []
    seen = set()
    for i, rec in enumerate(records, start=1):
        key = rec.get("id") if rec.get("id") is not None else (rec.get("nom"), rec.get("adresse"), rec.get("specialite"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rec)
        if i % 50 == 0:
            incremental_write_json(output_dir / f"{source_id}.json", deduped)

    stats = ScrapeStats(
        source_url=source_url,
        strategy=strategy,
        records=len(deduped),
        unique_ids=len({r.get('id') for r in deduped if r.get('id') is not None}),
        parsing_errors=len(errors),
    )
    return deduped, errors, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape ENDOSUD PACA specialists")
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID, help="Source identifier (default: SRC025)")
    parser.add_argument("--output-dir", default="outputs/SRC025", help="Output directory (default: outputs/SRC025)")
    parser.add_argument("--input-html", default=None, help="Path to local HTML file (e.g., endosudpaca.txt) instead of fetching from URL")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    source_id = args.source_id
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        records, errors, stats = scrape(source_id, output_dir, input_html=args.input_html)
    except requests.HTTPError as exc:
        logging.error("HTTP error: %s", exc)
        return 1
    except requests.RequestException as exc:
        logging.error("Request failed: %s", exc)
        return 1
    except FileNotFoundError as exc:
        logging.error("File error: %s", exc)
        return 1
    except Exception as exc:
        logging.exception("Unexpected failure: %s", exc)
        return 1

    json_path = output_dir / f"{source_id}.json"
    errors_path = output_dir / f"{source_id}_errors.json"
    summary_path = output_dir / f"{source_id}_summary.json"

    incremental_write_json(json_path, records)
    incremental_write_json(errors_path, errors)
    write_summary(summary_path, records, stats)

    logging.info("Saved %s records to %s", len(records), json_path)
    logging.info("Strategy used: %s", stats.strategy)
    logging.info("Parsing errors: %s", len(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())