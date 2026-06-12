import json
import time
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://fenm.org"
START_URL = "https://fenm.org/annuaire-des-professionnels"
OUTPUT_FILE = Path("SRC036.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_address(address_container):
    if not address_container:
        return {
            "full_address": "",
            "address_lines": [],
            "postal_code": "",
            "city": "",
            "country": "",
        }

    lines = []
    for span in address_container.select("span"):
        txt = clean_text(span.get_text(" ", strip=True))
        if txt:
            lines.append(txt)

    postal_code = ""
    city = ""
    country = ""

    postal = address_container.select_one(".postal-code")
    locality = address_container.select_one(".locality")
    country_el = address_container.select_one(".country")

    if postal:
        postal_code = clean_text(postal.get_text(" ", strip=True))
    if locality:
        city = clean_text(locality.get_text(" ", strip=True))
    if country_el:
        country = clean_text(country_el.get_text(" ", strip=True))

    full_address = ", ".join(lines)

    return {
        "full_address": full_address,
        "address_lines": lines,
        "postal_code": postal_code,
        "city": city,
        "country": country,
    }


def parse_card(card, page_number: int):
    name_link = card.select_one("h3 a")
    if not name_link:
        return None

    name = clean_text(name_link.get_text(" ", strip=True))
    profile_url = urljoin(BASE_URL, name_link.get("href", "").strip())

    specialty_el = card.select_one(".field--name-field-specialite")
    specialty = clean_text(specialty_el.get_text(" ", strip=True)) if specialty_el else ""

    address_el = card.select_one(".field--name-field-adresse .address")
    address_data = extract_address(address_el)

    phone_numbers = []
    for phone_el in card.select(".field--name-field-telephone a, .field--name-field-mobile a"):
        phone = clean_text(phone_el.get_text(" ", strip=True))
        if phone and phone not in phone_numbers:
            phone_numbers.append(phone)

    website_links = []
    for link_el in card.select(".field--name-field-lien a"):
        href = clean_text(link_el.get("href", ""))
        if href and href not in website_links:
            website_links.append(href)

    slug = ""
    href = name_link.get("href", "").strip()
    if href:
        slug = href.rstrip("/").split("/")[-1]

    return {
        "id": f"fenm_{slug or page_number}_{abs(hash(name)) % 100000}",
        "name": name,
        "specialty": specialty,
        "profile_url": profile_url,
        "address": address_data["full_address"],
        "address_lines": address_data["address_lines"],
        "postal_code": address_data["postal_code"],
        "city": address_data["city"],
        "country": address_data["country"],
        "phone_numbers": phone_numbers,
        "website_links": website_links,
        "page": page_number,
    }


def get_next_page_url(soup):
    next_link = soup.select_one("li.pager__item--next a")
    if not next_link:
        return None
    href = next_link.get("href", "").strip()
    if not href:
        return None
    return urljoin(BASE_URL, href)


def scrape_fenm_professionals():
    session = requests.Session()
    session.headers.update(HEADERS)

    all_results = []
    seen_profile_urls = set()

    current_url = START_URL
    page_number = 1

    while current_url:
        print(f"Scraping page {page_number}: {current_url}")

        response = session.get(current_url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        cards = soup.select("div.view-content ul > li.grid")
        print(f"  Found {len(cards)} professional cards.")

        if not cards:
            break

        added_this_page = 0

        for card in cards:
            item = parse_card(card, page_number)
            if not item:
                continue

            if item["profile_url"] in seen_profile_urls:
                continue

            seen_profile_urls.add(item["profile_url"])
            all_results.append(item)
            added_this_page += 1

        print(f"  Added {added_this_page} professionals.")

        next_page_url = get_next_page_url(soup)
        if not next_page_url or next_page_url == current_url:
            break

        current_url = next_page_url
        page_number += 1
        time.sleep(1)

    return all_results


def save_json(data, output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    data = scrape_fenm_professionals()
    save_json(data, OUTPUT_FILE)
    print(f"\nTotal professionals scraped: {len(data)}")
    print(f"Saved to {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()