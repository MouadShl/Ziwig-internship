#!/usr/bin/env python3
"""
EndAURA directory scraper.

Targets:
- Practitioner directory: https://www.endaura.fr/annuaire
- Expert centers listed on the same page

What it extracts:
- all surgeons / practitioners / doctors / sage-femmes from the annuaire
- all expert centers from the center selector
- specialty, profile URL, coordinates, addresses, phone numbers, map lat/lon
- optional detail-page enrichment when network access is available

Outputs:
- SRC032.json

This scraper can work in two modes:
1) Live mode: download https://www.endaura.fr/annuaire
2) Offline mode: parse a saved HTML copy of the annuaire page
   (default local file: "endo aura html.txt")
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.endaura.fr"
DIRECTORY_URL = f"{BASE_URL}/annuaire"
DEFAULT_HTML_FILE = "/mnt/data/endo aura html.txt"
OUTPUT_FILE = Path("outputs/SRC032/SRC032.json")
TIMEOUT = 30
SLEEP_SECONDS = 0.35

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
}


@dataclass
class CoordinateEntry:
    organization: str = ""
    address_line1: str = ""
    address_line2: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = ""
    phone: str = ""


class EndAuraScraper:
    def __init__(self, html_path: Optional[str] = None, fetch_detail_pages: bool = True) -> None:
        self.html_path = html_path
        self.fetch_detail_pages = fetch_detail_pages
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def get_html(self) -> str:
        if self.html_path and Path(self.html_path).exists():
            return Path(self.html_path).read_text(encoding="utf-8")
        response = self.session.get(DIRECTORY_URL, timeout=TIMEOUT)
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    @staticmethod
    def clean_text(text: Optional[str]) -> str:
        if not text:
            return ""
        text = unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip(" \t\r\n\xa0")

    def parse_directory(self, html: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")

        practitioners = self._parse_name_options(soup, "#recherche-block-1-jump-menu option[data-url]", "practitioner")
        centers = self._parse_name_options(soup, "#recherche-block-2-jump-menu option[data-url]", "center")

        self._apply_footer_rows(soup, practitioners)
        self._apply_leaflet_data(soup, practitioners)

        if self.fetch_detail_pages:
            self._enrich_detail_pages(practitioners)
            self._enrich_detail_pages(centers)

        practitioner_list = sorted(practitioners.values(), key=lambda x: self.sort_key(x))
        center_list = sorted(centers.values(), key=lambda x: self.sort_key(x))

        return {
            "metadata": {
                "source": "endaura.fr",
                "base_url": BASE_URL,
                "directory_url": DIRECTORY_URL,
                "html_source": self.html_path or DIRECTORY_URL,
                "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "detail_page_enrichment": self.fetch_detail_pages,
                "total_practitioners": len(practitioner_list),
                "total_centers": len(center_list),
            },
            "practitioners": practitioner_list,
            "expert_centers": center_list,
        }

    def _parse_name_options(self, soup: BeautifulSoup, selector: str, entry_type: str) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        for idx, opt in enumerate(soup.select(selector), start=1):
            rel = opt.get("data-url", "").strip()
            name = self.clean_text(opt.get_text(" ", strip=True))
            if not rel or not name:
                continue
            url = urljoin(BASE_URL, rel)
            slug = urlparse(url).path.strip("/")
            results[slug] = {
                "id": slug,
                "entry_type": entry_type,
                "name": name,
                "profile_url": url,
                "specialty": "",
                "specialty_raw": "",
                "coordinates": [],
                "phones": [],
                "emails": [],
                "websites": [],
                "centers": [],
                "level": "",
                "description": "",
                "image": "",
                "raw_popup_html": [],
                "source_rank": idx,
            }
        return results

    def _apply_footer_rows(self, soup: BeautifulSoup, records: Dict[str, Dict[str, Any]]) -> None:
        for row in soup.select(".view-footer .views-row"):
            a = row.select_one("a[href]")
            if not a:
                continue
            url = urljoin(BASE_URL, a.get("href", ""))
            slug = urlparse(url).path.strip("/")
            rec = records.get(slug)
            if not rec:
                continue
            rec["name"] = self.clean_text(a.get_text(" ", strip=True)) or rec["name"]
            speciality = row.select_one(".specialites")
            if speciality:
                spec = self.clean_text(speciality.get_text(" ", strip=True))
                if spec:
                    rec["specialty"] = spec
                    rec["specialty_raw"] = spec

    def _apply_leaflet_data(self, soup: BeautifulSoup, records: Dict[str, Dict[str, Any]]) -> None:
        settings_tag = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
        if not settings_tag or not settings_tag.string:
            return

        try:
            settings = json.loads(settings_tag.string)
        except json.JSONDecodeError:
            return

        leaflet = settings.get("leaflet", {})
        for map_key, map_data in leaflet.items():
            features = map_data.get("features", []) if isinstance(map_data, dict) else []
            for feat in features:
                popup = (feat or {}).get("popup", {})
                popup_html = popup.get("value", "") if isinstance(popup, dict) else ""
                if not popup_html:
                    continue

                popup_soup = BeautifulSoup(popup_html, "html.parser")
                a = popup_soup.select_one("a[href]")
                if not a:
                    continue
                url = urljoin(BASE_URL, a.get("href", ""))
                slug = urlparse(url).path.strip("/")
                rec = records.get(slug)
                if not rec:
                    continue

                rec["name"] = self.clean_text(a.get_text(" ", strip=True)) or rec["name"]
                spec = popup_soup.select_one(".specialites")
                if spec:
                    spec_text = self.clean_text(spec.get_text(" ", strip=True))
                    if spec_text:
                        rec["specialty"] = spec_text
                        rec["specialty_raw"] = spec_text

                lat = feat.get("lat")
                lon = feat.get("lon")
                if lat is not None and lon is not None:
                    rec.setdefault("map_points", [])
                    point = {"lat": lat, "lon": lon}
                    if point not in rec["map_points"]:
                        rec["map_points"].append(point)

                coords = self._extract_coordinates_from_popup(popup_soup)
                for coord in coords:
                    coord_dict = asdict(coord)
                    if coord_dict not in rec["coordinates"]:
                        rec["coordinates"].append(coord_dict)
                    phone = coord.phone
                    if phone and phone not in rec["phones"]:
                        rec["phones"].append(phone)
                    org = coord.organization
                    if org and org not in rec["centers"]:
                        rec["centers"].append(org)

                raw = self.clean_text(popup_html)
                if raw and raw not in rec["raw_popup_html"]:
                    rec["raw_popup_html"].append(raw)

    def _extract_coordinates_from_popup(self, popup_soup: BeautifulSoup) -> List[CoordinateEntry]:
        out: List[CoordinateEntry] = []
        for para in popup_soup.select(".paragraph--type--coordonnees"):
            coord = CoordinateEntry()
            coord.organization = self._text_of(para.select_one(".organization"))
            coord.address_line1 = self._text_of(para.select_one(".address-line1"))
            coord.address_line2 = self._text_of(para.select_one(".address-line2"))
            coord.postal_code = self._text_of(para.select_one(".postal-code"))
            coord.city = self._text_of(para.select_one(".locality"))
            coord.country = self._text_of(para.select_one(".country"))
            tel = para.select_one('a[href^="tel:"]')
            if tel:
                coord.phone = self.clean_text(tel.get_text(" ", strip=True)) or tel.get("href", "").replace("tel:", "")
            out.append(coord)
        return out

    def _text_of(self, tag: Optional[BeautifulSoup]) -> str:
        if not tag:
            return ""
        return self.clean_text(tag.get_text(" ", strip=True))

    def _enrich_detail_pages(self, records: Dict[str, Dict[str, Any]]) -> None:
        for rec in records.values():
            try:
                time.sleep(SLEEP_SECONDS)
                response = self.session.get(rec["profile_url"], timeout=TIMEOUT)
                response.raise_for_status()
                response.encoding = "utf-8"
                soup = BeautifulSoup(response.text, "html.parser")
                self._parse_detail_page(soup, rec)
            except Exception as exc:
                rec.setdefault("detail_errors", []).append(str(exc))

    def _parse_detail_page(self, soup: BeautifulSoup, rec: Dict[str, Any]) -> None:
        title = soup.select_one("h1")
        if title:
            rec["name"] = self.clean_text(title.get_text(" ", strip=True)) or rec["name"]

        # image
        for img in soup.select("img[src]"):
            src = img.get("src", "")
            if not src:
                continue
            if any(x in src.lower() for x in ["logo", "linkedin", "favicon"]):
                continue
            full = urljoin(BASE_URL, src)
            rec["image"] = full
            break

        # look for specialties / levels / generic field values
        text_blocks = []
        for sel in [
            ".field",
            ".paragraph",
            ".field__item",
            ".region-content p",
            ".region-content li",
        ]:
            for node in soup.select(sel):
                txt = self.clean_text(node.get_text(" ", strip=True))
                if txt and txt not in text_blocks:
                    text_blocks.append(txt)

        if not rec.get("specialty"):
            for txt in text_blocks:
                if any(k in txt.lower() for k in ["gynécologue", "radiologue", "sage-femme", "urologue", "médecin", "chirurg", "pharmacien"]):
                    rec["specialty"] = txt
                    rec["specialty_raw"] = txt
                    break

        for txt in text_blocks:
            low = txt.lower()
            if not rec.get("level") and ("membre expert" in low or "recours" in low or "niveau" in low):
                rec["level"] = txt
            if not rec.get("description") and len(txt) > 80 and not any(k in low for k in ["mentions légales", "plan du site", "actualités"]):
                rec["description"] = txt

        # emails / websites / phones
        for a in soup.select('a[href]'):
            href = a.get('href', '')
            label = self.clean_text(a.get_text(' ', strip=True))
            if href.startswith('mailto:'):
                email = href.replace('mailto:', '').strip()
                if email and email not in rec['emails']:
                    rec['emails'].append(email)
            elif href.startswith('tel:'):
                phone = label or href.replace('tel:', '').strip()
                if phone and phone not in rec['phones']:
                    rec['phones'].append(phone)
            elif href.startswith('http'):
                if 'endaura.fr' not in href and href not in rec['websites']:
                    rec['websites'].append(href)

        # look for explicit address paragraphs on detail page
        for para in soup.select('.paragraph--type--coordonnees'):
            coord = CoordinateEntry(
                organization=self._text_of(para.select_one('.organization')),
                address_line1=self._text_of(para.select_one('.address-line1')),
                address_line2=self._text_of(para.select_one('.address-line2')),
                postal_code=self._text_of(para.select_one('.postal-code')),
                city=self._text_of(para.select_one('.locality')),
                country=self._text_of(para.select_one('.country')),
            )
            tel = para.select_one('a[href^="tel:"]')
            if tel:
                coord.phone = self.clean_text(tel.get_text(' ', strip=True)) or tel.get('href', '').replace('tel:', '')
            coord_dict = asdict(coord)
            if coord_dict not in rec['coordinates']:
                rec['coordinates'].append(coord_dict)
            if coord.organization and coord.organization not in rec['centers']:
                rec['centers'].append(coord.organization)
            if coord.phone and coord.phone not in rec['phones']:
                rec['phones'].append(coord.phone)

    @staticmethod
    def sort_key(rec: Dict[str, Any]) -> Tuple[str, str]:
        return (rec.get("name", "").casefold(), rec.get("id", ""))


def main() -> None:
    local_html = DEFAULT_HTML_FILE if Path(DEFAULT_HTML_FILE).exists() else None
    scraper = EndAuraScraper(html_path=local_html, fetch_detail_pages=False if local_html else True)
    html = scraper.get_html()
    data = scraper.parse_directory(html)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {OUTPUT_FILE.resolve()}")
    print(f"Practitioners: {data['metadata']['total_practitioners']}")
    print(f"Expert centers: {data['metadata']['total_centers']}")


if __name__ == "__main__":
    main()
