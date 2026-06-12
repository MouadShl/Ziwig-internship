import json
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.endocentrevdl.fr"
ARCHIVE_URL = f"{BASE_URL}/professionnels/"
OUTPUT_FILE = Path("SRC035.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
REQUEST_DELAY = 0.5
TIMEOUT = 30

session = requests.Session()
session.headers.update(HEADERS)


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_lines(lines: List[str]) -> List[str]:
    return [clean_text(x) for x in lines if clean_text(x)]


def normalize_phone(text: str) -> str:
    return clean_text(text.replace("Tel:", "").replace("Tél:", ""))


def unique_keep_order(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def fetch_html(url: str) -> str:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def extract_archive_cards(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    cards = []

    # Main archive cards usually have a heading + "Voir la fiche" link.
    for link in soup.select('a[href*="/professionnel/"]'):
        href = link.get("href", "")
        full_url = urljoin(BASE_URL, href)
        card = link.find_parent()
        if not card:
            continue

        # climb a little to include surrounding text for specialty / department / city
        container = card
        for _ in range(4):
            if not container:
                break
            text = clean_text(container.get_text(" ", strip=True))
            if "Voir la fiche" in text and len(text) < 400:
                card = container
                break
            container = container.parent

        text_lines = clean_lines(card.get_text("\n", strip=True).splitlines())
        if not any("Voir la fiche" in line for line in text_lines):
            continue

        heading = card.find(["h2", "h3", "h4"])
        name = clean_text(heading.get_text(" ", strip=True)) if heading else clean_text(link.get_text(" ", strip=True))
        if not name or name.lower() == "voir la fiche":
            continue

        filtered = [line for line in text_lines if line != "Voir la fiche" and line != name]
        specialty = filtered[0] if len(filtered) > 0 else ""
        departments = []
        cities = []
        for line in filtered[1:]:
            if re.search(r"\(\d{2}\)", line):
                departments.append(line)
            else:
                cities.append(line)

        cards.append(
            {
                "name": name,
                "profile_url": full_url,
                "specialty_archive": specialty,
                "departments_archive": unique_keep_order(departments),
                "cities_archive": unique_keep_order(cities),
            }
        )

    deduped = []
    seen_urls = set()
    for card in cards:
        if card["profile_url"] in seen_urls:
            continue
        seen_urls.add(card["profile_url"])
        deduped.append(card)
    return deduped


def extract_section_text(soup: BeautifulSoup, section_title: str) -> List[str]:
    heading = None
    for tag in soup.find_all(["h2", "h3"]):
        if clean_text(tag.get_text(" ", strip=True)).lower() == section_title.lower():
            heading = tag
            break
    if not heading:
        return []

    lines: List[str] = []
    node = heading.find_next_sibling()
    while node:
        if node.name in ["h2"]:
            break
        if node.name in ["h3"] and clean_text(node.get_text(" ", strip=True)).lower() in {
            "adresse", "par téléphone", "en ligne"
        }:
            lines.append(f"__SUBHEAD__:{clean_text(node.get_text(' ', strip=True))}")
        else:
            chunk = clean_text(node.get_text("\n", strip=True))
            if chunk:
                lines.extend(clean_lines(chunk.splitlines()))
        node = node.find_next_sibling()
    return lines


def parse_contact_block(lines: List[str]) -> Dict[str, Any]:
    phones: List[str] = []
    emails: List[str] = []
    booking_links: List[Dict[str, str]] = []
    notes: List[str] = []
    current_mode: Optional[str] = None

    for line in lines:
        if line.startswith("__SUBHEAD__:"):
            current_mode = line.split(":", 1)[1].strip().lower()
            continue

        found_emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", line)
        emails.extend(found_emails)

        phone_matches = re.findall(r"(?:\+33\s?|0)[0-9](?:[\s.\-]?[0-9]{2}){4}|0[0-9](?:[\s.\-]?[0-9]){8}|(?:\d{2}[\s.\-]?){5}", line)
        phones.extend([normalize_phone(x) for x in phone_matches])

        url_matches = re.findall(r"https?://\S+", line)
        for url in url_matches:
            booking_links.append({
                "label": "Prendre rendez-vous" if current_mode == "en ligne" else "Lien",
                "url": url.rstrip(').,;'),
            })

        if not phone_matches and not found_emails and not url_matches:
            notes.append(line)

    return {
        "phones": unique_keep_order(phones),
        "emails": unique_keep_order(emails),
        "booking_links": booking_links,
        "contact_notes": unique_keep_order(notes),
    }


def parse_profile(url: str, archive_card: Dict[str, Any]) -> Dict[str, Any]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    name_tag = soup.find("h1")
    name = clean_text(name_tag.get_text(" ", strip=True)) if name_tag else archive_card.get("name", "")

    meta_lines = []
    if name_tag:
        node = name_tag.find_next_sibling()
        while node and len(meta_lines) < 3:
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                meta_lines.append(text)
            node = node.find_next_sibling()

    specialty = meta_lines[0] if len(meta_lines) > 0 else archive_card.get("specialty_archive", "")
    location_line = meta_lines[1] if len(meta_lines) > 1 else ""
    recours_level = ""
    if len(meta_lines) > 2 and "recours" in meta_lines[2].lower():
        recours_level = meta_lines[2]
    elif len(meta_lines) > 2:
        # keep it anyway if it looks useful
        recours_level = meta_lines[2]

    departments = archive_card.get("departments_archive", []).copy()
    cities = archive_card.get("cities_archive", []).copy()
    if location_line:
        parts = [clean_text(x) for x in location_line.split("-")]
        if len(parts) >= 2:
            departments = unique_keep_order([parts[0]] + departments)
            cities = unique_keep_order([parts[1]] + cities)
        else:
            departments = unique_keep_order([location_line] + departments)

    competencies = [line for line in extract_section_text(soup, "Compétences") if not line.startswith("__SUBHEAD__:")]
    lieu_lines = [line for line in extract_section_text(soup, "Lieu") if not line.startswith("__SUBHEAD__:") and line.lower() != "adresse"]
    contact_lines = extract_section_text(soup, "Prendre rendez-vous")
    contact_data = parse_contact_block(contact_lines)

    address_lines = lieu_lines
    postal_codes = sorted(set(re.findall(r"\b\d{5}\b", " ".join(address_lines))))

    external_links: List[str] = []
    for a in soup.select('a[href]'):
        href = a.get('href', '')
        full = urljoin(url, href)
        if full.startswith(BASE_URL):
            continue
        if any(domain in full for domain in ['facebook.com', 'instagram.com', 'linkedin.com', 'endofrance.org', 'endomind.org', 'mediapilote.com']):
            continue
        external_links.append(full)
    external_links = unique_keep_order(external_links)

    image_url = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        image_url = og["content"]

    return {
        "name": name,
        "profile_url": url,
        "specialty": specialty,
        "recours_level": recours_level,
        "departments": departments,
        "cities": cities,
        "competencies": competencies,
        "address_lines": address_lines,
        "postal_codes": postal_codes,
        "phones": contact_data["phones"],
        "emails": contact_data["emails"],
        "booking_links": contact_data["booking_links"],
        "contact_notes": contact_data["contact_notes"],
        "external_links": external_links,
        "image": image_url,
        "archive": archive_card,
    }


def scrape() -> Dict[str, Any]:
    html = fetch_html(ARCHIVE_URL)
    soup = BeautifulSoup(html, "html.parser")
    cards = extract_archive_cards(soup)

    records = []
    for idx, card in enumerate(cards, start=1):
        print(f"[{idx}/{len(cards)}] Scraping {card['name']} -> {card['profile_url']}")
        try:
            record = parse_profile(card["profile_url"], card)
            records.append(record)
        except Exception as exc:
            records.append({
                "name": card.get("name", ""),
                "profile_url": card.get("profile_url", ""),
                "archive": card,
                "error": str(exc),
            })
        time.sleep(REQUEST_DELAY)

    return {
        "source_url": ARCHIVE_URL,
        "total_profiles": len(records),
        "items": records,
    }


def main() -> None:
    data = scrape()
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved to {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
