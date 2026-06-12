#!/usr/bin/env python3
import argparse
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://endo-bfc.fr"
START_URL = "https://endo-bfc.fr/annuaire/"
SOURCE_ID = "SRC022"
SOURCE_NAME = "Endo BFC"
SOURCE_COUNTRY = "France"
SOURCE_LANGUAGE = "fr"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

PHONE_RE = re.compile(r"(?:(?:\+33|0)[\s.\-]?\d(?:[\s.\-]?\d{2}){4})")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_url(url: str, base: str = BASE_URL) -> str:
    if not url:
        return ""
    return urljoin(base, url)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class EndoBFCScraper:
    def __init__(self, output_dir: str, sleep_seconds: float = 1.2):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.articles_file = self.output_dir / f"{SOURCE_ID}_articles_final.jsonl"
        self.errors_file = self.output_dir / f"{SOURCE_ID}_errors_final.jsonl"

        self.sleep_seconds = sleep_seconds
        self.session = build_session()
        self.logger = logging.getLogger(self.__class__.__name__)

        self.seen_keys = set()
        self.saved_count = 0
        self.error_count = 0

    def sleep(self):
        time.sleep(self.sleep_seconds)

    def fetch(self, url: str) -> str:
        self.sleep()
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text

    def safe_fetch(self, url: str) -> str:
        try:
            return self.fetch(url)
        except Exception as exc:
            self.log_error({"url": url, "error": str(exc)})
            self.logger.warning("Failed GET %s -> %s", url, exc)
            return ""

    def log_error(self, payload: dict):
        self.error_count += 1
        row = {"source_id": SOURCE_ID, **payload}
        with open(self.errors_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_row(self, row: dict):
        with open(self.articles_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.saved_count += 1

    def parse_listing_cards(self, html: str):
        soup = BeautifulSoup(html, "lxml")

        cards = soup.select("article, .annuaire-item, .praticien, .jet-listing-grid__item")
        if not cards:
            cards = soup.select("a[href*='/professionnels/']")

        out = []

        for card in cards:
            link_el = card if getattr(card, "name", None) == "a" else card.select_one("a[href*='/professionnels/']")
            if not link_el:
                continue

            profile_url = normalize_url(link_el.get("href", ""))
            if "/professionnels/" not in profile_url:
                continue

            text_blob = clean_text(card.get_text(" ", strip=True))

            name = ""
            specialty = ""
            address = ""
            phone = ""

            heading = card.select_one("h1, h2, h3, h4, .elementor-heading-title, .jet-listing-dynamic-field__content")
            if heading:
                name = clean_text(heading.get_text(" ", strip=True))

            all_lines = [clean_text(x) for x in card.get_text("\n", strip=True).split("\n") if clean_text(x)]

            if not name and all_lines:
                name = all_lines[0]

            if len(all_lines) >= 2 and not specialty:
                specialty = all_lines[1]

            phone_match = PHONE_RE.search(text_blob)
            if phone_match:
                phone = clean_text(phone_match.group(0))

            address_lines = []
            for line in all_lines:
                low = line.lower()
                if line == name or line == specialty:
                    continue
                if PHONE_RE.search(line):
                    continue
                if EMAIL_RE.search(line):
                    continue
                if "http" in low or "www." in low:
                    continue
                if any(token in low for token in [
                    "voir le praticien", "lire la suite", "en savoir plus", "site web"
                ]):
                    continue
                if len(line) < 5:
                    continue
                address_lines.append(line)

            if address_lines:
                address = ", ".join(address_lines)

            out.append({
                "name": name,
                "specialty": specialty,
                "address": address,
                "phone": phone,
                "profile_url": profile_url,
            })

        deduped = []
        seen = set()
        for row in out:
            key = row["profile_url"]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    def extract_profile_name(self, soup: BeautifulSoup, fallback: str) -> str:
        for sel in ["h1", "h2", ".elementor-heading-title"]:
            el = soup.select_one(sel)
            if el:
                text = clean_text(el.get_text(" ", strip=True))
                if text:
                    return text
        return fallback

    def extract_profile_specialty(self, soup: BeautifulSoup, fallback: str) -> str:
        texts = []
        for el in soup.select("h2, h3, h4, p, li, div"):
            txt = clean_text(el.get_text(" ", strip=True))
            if txt:
                texts.append(txt)

        for txt in texts:
            low = txt.lower()
            if any(x in low for x in [
                "gynécologue", "gynécologie", "ostéopathe", "ostéopathie",
                "sage-femme", "radiologue", "urologue", "algologue",
                "psychologue", "médecin généraliste", "masseur kinésithérapeute",
                "chirurgie viscérale", "diététiciens", "nutritionniste",
                "centre périnatal"
            ]):
                return txt
        return fallback

    def extract_profile_address(self, soup: BeautifulSoup, fallback: str) -> str:
        candidates = []

        for el in soup.select("p, li, div"):
            text = clean_text(el.get_text(" ", strip=True))
            if not text:
                continue

            low = text.lower()

            if PHONE_RE.search(text):
                continue
            if EMAIL_RE.search(text):
                continue
            if "facebook.com" in low or "http" in low or "www." in low:
                continue

            if any(token in low for token in [
                "adresse", "rue", "route", "avenue", "av.", "boulevard", "bd",
                "chemin", "place", "allée", "lotissement"
            ]) or re.search(r"\b\d{5}\b", text):
                candidates.append(text)

        if candidates:
            cleaned = []
            for text in candidates:
                text = re.sub(r"^\s*adresse\s*:?\s*", "", text, flags=re.I)
                text = clean_text(text)
                if text and text not in cleaned:
                    cleaned.append(text)
            return " | ".join(cleaned)

        return fallback

    def extract_profile_phone(self, soup: BeautifulSoup, fallback: str) -> str:
        tel = soup.select_one("a[href^='tel:']")
        if tel:
            return clean_text(tel.get_text(" ", strip=True)) or clean_text(tel.get("href", "").replace("tel:", ""))

        text = clean_text(soup.get_text(" ", strip=True))
        m = PHONE_RE.search(text)
        if m:
            return clean_text(m.group(0))
        return fallback

    def extract_profile_email(self, soup: BeautifulSoup) -> str:
        mail = soup.select_one("a[href^='mailto:']")
        if mail:
            return clean_text(mail.get("href", "").replace("mailto:", ""))

        text = clean_text(soup.get_text(" ", strip=True))
        m = EMAIL_RE.search(text)
        if m:
            return clean_text(m.group(0))
        return ""

    def extract_profile_website(self, soup: BeautifulSoup) -> str:
        bad_domains = {"facebook.com", "www.facebook.com", "endo-bfc.fr"}
        for a in soup.select("a[href]"):
            href = normalize_url(a.get("href", ""))
            if not href:
                continue
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if not host:
                continue
            if "/professionnels/" in href:
                continue
            if any(host.endswith(bad) for bad in bad_domains):
                continue
            if host != "endo-bfc.fr":
                return href
        return ""

    def parse_profile(self, profile_url: str, listing_data: dict) -> dict:
        html = self.safe_fetch(profile_url)
        if not html:
            return listing_data.copy()

        soup = BeautifulSoup(html, "lxml")

        name = self.extract_profile_name(soup, listing_data.get("name", ""))
        specialty = self.extract_profile_specialty(soup, listing_data.get("specialty", ""))
        address = self.extract_profile_address(soup, listing_data.get("address", ""))
        phone = self.extract_profile_phone(soup, listing_data.get("phone", ""))
        email = self.extract_profile_email(soup)
        website = self.extract_profile_website(soup)

        return {
            "name": name,
            "specialty": specialty,
            "address": address,
            "phone": phone,
            "email": email or None,
            "website": website or None,
            "profile_url": profile_url,
        }

    def make_record(self, idx: int, practitioner: dict) -> dict:
        return {
            "source_id": SOURCE_ID,
            "source_mode": "association_directory",
            "source_name": SOURCE_NAME,
            "source_country": SOURCE_COUNTRY,
            "source_language": SOURCE_LANGUAGE,
            "id": idx,
            "name": practitioner.get("name", ""),
            "specialty": practitioner.get("specialty", ""),
            "address": practitioner.get("address", ""),
            "phone": practitioner.get("phone", "") or None,
            "email": practitioner.get("email"),
            "website": practitioner.get("website"),
            "profile_url": practitioner.get("profile_url", ""),
        }

    def run(self):
        self.logger.info("Starting %s scraper", SOURCE_ID)
        self.logger.info("Listing URL: %s", START_URL)
        self.logger.info("Output dir: %s", self.output_dir)

        html = self.safe_fetch(START_URL)
        if not html:
            self.logger.error("Could not fetch listing page.")
            return

        listing_cards = self.parse_listing_cards(html)
        self.logger.info("Found %s listing cards", len(listing_cards))

        records = []
        seq = 1

        for listing_data in listing_cards:
            try:
                practitioner = self.parse_profile(listing_data["profile_url"], listing_data)

                dedupe_key = (
                    clean_text(practitioner.get("name", "")).lower(),
                    clean_text(practitioner.get("address", "")).lower(),
                )
                if dedupe_key in self.seen_keys:
                    continue
                self.seen_keys.add(dedupe_key)

                record = self.make_record(seq, practitioner)
                records.append(record)
                self.write_row(record)
                seq += 1

                if seq % 10 == 0:
                    self.logger.info("Saved %s practitioners so far", seq - 1)

            except Exception as exc:
                self.log_error({
                    "profile_url": listing_data.get("profile_url", ""),
                    "error": str(exc),
                })
                self.logger.warning("Profile parse failed: %s", exc)

        self.logger.info("Done")
        self.logger.info("saved_records=%s", len(records))
        self.logger.info("articles_file=%s", self.articles_file)
        self.logger.info("errors_file=%s", self.errors_file)


def main():
    parser = argparse.ArgumentParser(description="Scrape https://endo-bfc.fr/annuaire/")
    parser.add_argument(
        "--output-dir",
        default=f"outputs/{SOURCE_ID}",
        help="Directory where SRC022_articles_final.jsonl will be written",
    )
    parser.add_argument("--sleep", type=float, default=1.2, help="Delay between requests in seconds")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    scraper = EndoBFCScraper(output_dir=args.output_dir, sleep_seconds=args.sleep)
    scraper.run()


if __name__ == "__main__":
    main()