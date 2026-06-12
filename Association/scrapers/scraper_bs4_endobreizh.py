#!/usr/bin/env python3
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.endobreizh.com"
LIST_URL = f"{BASE_URL}/professionnels/"
OUTPUT_FILE = Path("SRC033.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def unique_keep_order(items):
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    response = session.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def extract_archive_links(soup: BeautifulSoup):
    profiles = []
    seen = set()

    for a in soup.select('a[href*="/professionnel/"]'):
        href = a.get("href", "").strip()
        if not href:
            continue
        full_url = urljoin(BASE_URL, href)
        if full_url in seen:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        card = a.find_parent()
        specialty = ""
        department = ""
        city = ""

        if card:
            span_candidates = card.find_all(["span", "li", "p", "div"], limit=10)
            snippets = [clean_text(x.get_text(" ", strip=True)) for x in span_candidates]
            snippets = [s for s in snippets if s and s != title and s != "VOIR LA FICHE"]
            for s in snippets:
                if not specialty and any(k in s.lower() for k in [
                    "sage-femme", "radiologue", "gynécologie", "médecin", "kinésithérapeute",
                    "diététique", "nutrition", "algologue", "chirurgie", "sexologue"
                ]):
                    specialty = s
                elif not department and re.match(r"^\d{2}\s*-", s):
                    department = s
                elif not city:
                    city = s

        profiles.append({
            "name_from_archive": title,
            "profile_url": full_url,
            "specialty_from_archive": specialty,
            "department_from_archive": department,
            "city_from_archive": city,
        })
        seen.add(full_url)

    return profiles


def find_heading_node(soup: BeautifulSoup, pattern: str):
    regex = re.compile(pattern, re.I)
    for node in soup.find_all(["h1", "h2", "h3", "h4", "strong", "p", "div"]):
        txt = clean_text(node.get_text(" ", strip=True))
        if txt and regex.search(txt):
            return node
    return None


def gather_text_until_next_heading(start_node):
    values = []
    if not start_node:
        return values

    for sib in start_node.find_next_siblings():
        if sib.name in {"h1", "h2", "h3", "h4"}:
            break
        txt = clean_text(sib.get_text(" ", strip=True))
        if txt:
            values.append(txt)
    return values


def extract_phone_numbers(text: str):
    phones = re.findall(r"(?:\+33\s?[1-9](?:[ .-]?\d{2}){4}|0[1-9](?:[ .-]?\d{2}){4}|0\d(?:\s?\d{2}){4})", text)
    return unique_keep_order([clean_text(p) for p in phones])


def extract_emails(text: str):
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return unique_keep_order(emails)


def parse_addresses(detail_text: str):
    addresses = []
    current = None

    for raw_line in detail_text.splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        if re.match(r"^Adresse\s+\d+\s*:", line, re.I):
            if current:
                addresses.append(current)
            current = {
                "label": line,
                "full_text": "",
                "phones": [],
                "emails": [],
                "booking_links": [],
                "postal_code": "",
                "city": "",
            }
            continue
        if current is None:
            continue
        if current["full_text"]:
            current["full_text"] += " | " + line
        else:
            current["full_text"] = line

    if current:
        addresses.append(current)

    for addr in addresses:
        addr["phones"] = extract_phone_numbers(addr["full_text"])
        addr["emails"] = extract_emails(addr["full_text"])
        m = re.search(r"\b(\d{5})\b\s*([A-Za-zÀ-ÿ'\- ]+)$", addr["full_text"])
        if m:
            addr["postal_code"] = m.group(1)
            addr["city"] = clean_text(m.group(2))
    return addresses


def parse_detail_page(session: requests.Session, profile: dict):
    soup = get_soup(session, profile["profile_url"])
    full_text = soup.get_text("\n", strip=True)

    title = clean_text((soup.select_one("h1") or soup.select_one("title")).get_text(" ", strip=True))
    subtitle = clean_text((soup.select_one("h2") or soup.select_one(".elementor-heading-title") or soup.select_one(".entry-subtitle") or soup.select_one("h3")).get_text(" ", strip=True)) if (soup.select_one("h2") or soup.select_one(".elementor-heading-title") or soup.select_one(".entry-subtitle") or soup.select_one("h3")) else ""

    image = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        image = urljoin(BASE_URL, og["content"])
    else:
        img = soup.select_one("img")
        if img and img.get("src"):
            image = urljoin(BASE_URL, img.get("src"))

    speciality_heading = find_heading_node(soup, r"Spécialité")
    expertise_heading = find_heading_node(soup, r"Expertise")
    structures_heading = find_heading_node(soup, r"Structures? de travail")
    niveaux_heading = find_heading_node(soup, r"Niveaux? de soin")

    speciality_block = gather_text_until_next_heading(speciality_heading)
    expertise_block = gather_text_until_next_heading(expertise_heading)
    structures_block = gather_text_until_next_heading(structures_heading)
    niveaux_block = gather_text_until_next_heading(niveaux_heading)

    specialty = profile.get("specialty_from_archive") or (speciality_block[0] if speciality_block else "")
    expertise = expertise_block
    structures = structures_block
    niveaux = niveaux_block

    addresses = parse_addresses(full_text)

    booking_links = []
    external_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        label = clean_text(a.get_text(" ", strip=True))
        full_url = urljoin(profile["profile_url"], href)
        if any(x in full_url.lower() for x in ["rdv", "doctolib", "prendre-mon-rdv"]):
            booking_links.append({"label": label or "Prendre rendez-vous", "url": full_url})
        elif full_url.startswith("http") and "endobreizh.com" not in full_url:
            external_links.append({"label": label, "url": full_url})

    phones = extract_phone_numbers(full_text)
    emails = extract_emails(full_text)

    dept_regions = re.findall(r"\b\d{2}\s*-\s*[^\n|]+", full_text)
    cities = []
    for addr in addresses:
        if addr.get("city"):
            cities.append(addr["city"])
    if profile.get("city_from_archive"):
        cities.extend([clean_text(x) for x in re.split(r",|/", profile["city_from_archive"])])
    cities = unique_keep_order([c for c in cities if c])

    return {
        "name": title,
        "subtitle": subtitle,
        "profile_url": profile["profile_url"],
        "image": image,
        "specialty": specialty,
        "department_regions": unique_keep_order([profile.get("department_from_archive", "")] + dept_regions),
        "cities": cities,
        "structures": structures,
        "levels_of_care": niveaux,
        "expertise": expertise,
        "addresses": addresses,
        "phones": phones,
        "emails": emails,
        "booking_links": unique_keep_order([json.dumps(x, ensure_ascii=False) for x in booking_links]),
        "external_links": unique_keep_order([json.dumps(x, ensure_ascii=False) for x in external_links]),
        "source_archive": {
            "name": profile.get("name_from_archive", ""),
            "specialty": profile.get("specialty_from_archive", ""),
            "department": profile.get("department_from_archive", ""),
            "city": profile.get("city_from_archive", ""),
        },
    }


def normalize_record(record: dict):
    record["booking_links"] = [json.loads(x) for x in record.get("booking_links", [])]
    record["external_links"] = [json.loads(x) for x in record.get("external_links", [])]
    return record


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    archive_soup = get_soup(session, LIST_URL)
    profiles = extract_archive_links(archive_soup)
    print(f"Found {len(profiles)} profile links")

    records = []
    for idx, profile in enumerate(profiles, start=1):
        print(f"[{idx}/{len(profiles)}] {profile['profile_url']}")
        try:
            record = parse_detail_page(session, profile)
            records.append(normalize_record(record))
        except Exception as exc:
            records.append({
                "name": profile.get("name_from_archive", ""),
                "profile_url": profile["profile_url"],
                "specialty": profile.get("specialty_from_archive", ""),
                "department_regions": [profile.get("department_from_archive", "")],
                "cities": [profile.get("city_from_archive", "")],
                "error": str(exc),
            })
        time.sleep(0.8)

    output = {
        "metadata": {
            "source": "endobreizh.com",
            "base_url": BASE_URL,
            "archive_url": LIST_URL,
            "total_profiles": len(records),
        },
        "professionals": records,
    }

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(records)} profiles to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
