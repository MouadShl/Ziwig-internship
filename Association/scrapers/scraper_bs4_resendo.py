#!/usr/bin/env python3
"""
Scraper for https://www.resendo.fr/professionnels-de-sante

Strategy
--------
This page behaves like a long directory page rather than a paginated article list.
The scraper downloads the page with requests, extracts normalized text lines from the
main document, then parses those lines into professional records grouped by:
- network section (city correspondents vs Saint-Joseph hospital team)
- specialty heading
- geographic region (PARIS / ILE-DE-FRANCE / PROVINCE)

The parsing is text-driven on purpose because the visible directory content is rendered
as long blocks of text, with repeated "Retour" anchors and mixed formatting.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.resendo.fr"
TARGET_URL = "https://www.resendo.fr/professionnels-de-sante"
DEFAULT_OUTPUT = "outputs/SRC024/SRC24.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

SPECIALTY_ALIASES = {
    "Gynécologue Médical": {"Gynécologue Médical", "Gynecologue Medicale"},
    "Centre AMP": {"Centre AMP", "Medecins PMA"},
    "Médecin Anti-douleur": {"Médecin Anti-douleur", "Medecis Anti-douleur"},
    "Radiologue (échographie, IRM)": {
        "Radiologue (échographie, IRM)",
        "Imagerie Medicale",
    },
    "Chirurgien Gynécologique": {"Chirurgien Gynécologique"},
    "Urologue": {"Urologue"},
    "Médecin Généraliste": {"Médecin Généraliste"},
    "Médecin de la fertilité": {"Médecin de la fertilité"},
    "Psychologue / Psychiatre / Psychothérapeute / Psychanalyste": {
        "Psychologue . Psychiatre . Psychothérapeute . Psychanalyste",
        "Psychologues pshychitres psychotherapeute, psychanalystes",
    },
    "Kinésithérapeute": {"Kinésithérapeute"},
    "Diététicienne": {"Diététicienne"},
    "Micronutrionniste": {"Micronutrionniste", "Micronutritionniste"},
    "Ostéopathe": {"Ostéopathe"},
    "Sexologue": {"Sexologue"},
    "Sophrologue": {"Sophrologue", "Sophrologie"},
    "Sage-femme": {"Sage-femme", "Sages femmes"},
    "Ergothérapeute": {"Ergothérapeute", "ergotherapeuthe"},
    "Réflexologue": {"Réflexologue", "reflexologue"},
    "Hypnothérapeute": {"Hypnothérapeute", "hypnotherapeute"},
    "Médecins Hôpital Saint-Joseph": {
        "Médecins Hôpital Saint-Joseph",
        "Medecins de l hopita saint joseph",
    },
}

REGIONS = {
    "PARIS": "Paris",
    "ILE-DE-FRANCE": "Île-de-France",
    "ILE DE FRANCE": "Île-de-France",
    "Ile -de-France": "Île-de-France",
    "PROVINCE": "Province",
}

SKIP_EXACT = {
    "Accueil",
    "Endométriose",
    "Professionnels de Santé",
    "F.A.Q",
    "Contact",
    "Actualités",
    "Nous rejoindre",
    "Associations",
    "Retour",
    "cliquez ici )",
    "Médecins Correspondants en Ville",
}

NON_NAME_KEYWORDS = {
    "rue", "avenue", "boulevard", "bd", "allée", "route", "square", "place",
    "cabinet", "centre", "clinique", "hôpital", "hopital", "institut", "maison",
    "doctolib", "teleconsultation", "téléconsultation", "maiia", "keldoc", "médoucine",
    "paris", "province", "france", "française", "cedex", "croix-rouge", "pmi", "cpef",
    "sage", "gynécologue", "gynéco", "psychologue", "sexologue", "sophrologue",
    "ostéopathe", "radiologue", "algologue", "consultation", "endométriose",
    "groupe", "hospitalier", "médical", "medical", "centre", "service", "chirurgie",
    "urologique", "digestive", "cardiologie", "anatomopathologie", "imagerie",
    "médecin", "medecin", "pneumologie", "préconceptionnelle", "acupuncture",
}

PHONE_RE = re.compile(r"(?:\+33\s?)?(?:0\s?\d(?:[ .-]?\d{2}){4}|0\d(?:[ .-]?\d){8,9})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+(?:@|\[at\])[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
POSTAL_CITY_RE = re.compile(r"\b(\d{5})\b\s+(.+)$")
DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?", re.I)


@dataclass
class ProfessionalRecord:
    source_id: str = "SRC24"
    source_mode: str = "association_directory"
    source_name: str = "RESENDO"
    source_country: str = "France"
    source_language: str = "fr"
    listing_url: str = TARGET_URL
    network_section: Optional[str] = None
    specialty: Optional[str] = None
    region: Optional[str] = None
    name: Optional[str] = None
    professional_type: str = "health_professional"
    title: Optional[str] = None
    organization: Optional[str] = None
    address_lines: List[str] = field(default_factory=list)
    postal_code: Optional[str] = None
    city: Optional[str] = None
    phones: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    websites: List[str] = field(default_factory=list)
    booking_info: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw_lines: List[str] = field(default_factory=list)

    @property
    def full_address(self) -> str:
        parts = [x for x in self.address_lines if x]
        if self.postal_code and self.city:
            parts.append(f"{self.postal_code} {self.city}")
        elif self.postal_code:
            parts.append(self.postal_code)
        return ", ".join(parts)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["full_address"] = self.full_address
        data["professional_id"] = slugify(f"{self.specialty or ''}-{self.region or ''}-{self.name or ''}")
        return data


class ResendoScraper:
    def __init__(self, sleep_seconds: float = 1.0):
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=45)
        response.raise_for_status()
        time.sleep(self.sleep_seconds)
        return response.text

    def extract_lines(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        text = soup.get_text("\n", strip=True)
        raw_lines = [normalize_space(line) for line in text.splitlines()]

        lines: List[str] = []
        started = False
        for line in raw_lines:
            if not line:
                continue
            if line == "Médecins Correspondants en Ville":
                started = True
            if not started:
                continue
            if line in {"Mentions Légales", "Legal Mentions"}:
                break
            if line in SKIP_EXACT:
                continue
            if line.startswith("(...)") or line.startswith("- (...)"):
                continue
            lines.append(line)
        return lines

    def parse(self, lines: List[str]) -> List[ProfessionalRecord]:
        records: List[ProfessionalRecord] = []
        current_specialty: Optional[str] = None
        current_region: Optional[str] = None
        current_network = "Médecins correspondants en ville"
        buffer: List[str] = []

        def flush_buffer() -> None:
            nonlocal buffer
            if not buffer:
                return
            record = self.parse_record(buffer, current_specialty, current_region, current_network)
            if record and record.name:
                records.append(record)
            buffer = []

        for line in lines:
            normalized_specialty = match_specialty(line)
            if normalized_specialty:
                flush_buffer()
                current_specialty = normalized_specialty
                if normalized_specialty == "Médecins Hôpital Saint-Joseph":
                    current_network = "Hôpital Saint-Joseph"
                    current_region = "Paris"
                continue

            normalized_region = match_region(line)
            if normalized_region:
                flush_buffer()
                current_region = normalized_region
                continue

            if line == "Centre de l’endométriose" and current_network != "Hôpital Saint-Joseph":
                # footer repeat; stop parsing the city directory if we already scraped hospital section
                pass

            if is_new_name_line(line, current_specialty):
                flush_buffer()
                buffer = [line]
                continue

            if buffer:
                buffer.append(line)

        flush_buffer()
        return dedupe_records(records)

    def parse_record(
        self,
        lines: List[str],
        specialty: Optional[str],
        region: Optional[str],
        network_section: Optional[str],
    ) -> Optional[ProfessionalRecord]:
        if not lines:
            return None

        name = clean_name(lines[0])
        if not name:
            return None

        record = ProfessionalRecord(
            network_section=network_section,
            specialty=specialty,
            region=region,
            name=name,
            raw_lines=lines[:],
        )

        for raw in lines[1:]:
            line = normalize_space(raw.lstrip(">"))
            if not line:
                continue

            phones = PHONE_RE.findall(line)
            emails = EMAIL_RE.findall(line)
            domains = extract_domains(line)

            if phones:
                record.phones.extend(clean_phone(p) for p in phones)
                cleaned = line
                for p in phones:
                    cleaned = cleaned.replace(p, "")
                cleaned = cleaned.strip(" ,.-")
                if cleaned:
                    if looks_like_booking(cleaned):
                        record.booking_info.append(cleaned)
                    else:
                        record.notes.append(cleaned)
                continue

            if emails:
                record.emails.extend(e.replace("[at]", "@").replace("(at)", "@").lower() for e in emails)
                continue

            if domains:
                record.websites.extend(domains)
                continue

            postal_match = POSTAL_CITY_RE.search(line)
            if postal_match:
                record.postal_code = postal_match.group(1)
                record.city = normalize_space(postal_match.group(2))
                continue

            if looks_like_booking(line):
                record.booking_info.append(line)
                continue

            if looks_like_address(line):
                record.address_lines.append(line)
                continue

            if record.title is None and looks_like_title(line):
                record.title = line
                continue

            if record.organization is None and looks_like_organization(line):
                record.organization = line
                continue

            record.notes.append(line)

        record.phones = unique_preserve(record.phones)
        record.emails = unique_preserve(record.emails)
        record.websites = unique_preserve(record.websites)
        record.booking_info = unique_preserve(record.booking_info)
        record.address_lines = unique_preserve(record.address_lines)
        record.notes = unique_preserve(record.notes)
        return record

    def run(self) -> List[dict]:
        html = self.fetch_html(TARGET_URL)
        lines = self.extract_lines(html)
        records = self.parse(lines)
        scraped_at = datetime.now(timezone.utc).isoformat()
        return [
            {
                "site": "RESENDO",
                "source_id": "SRC24",
                "source_mode": "association_directory",
                "source_url": TARGET_URL,
                "scraped_at": scraped_at,
                **record.to_dict(),
            }
            for record in records
        ]


def normalize_space(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def unique_preserve(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        cleaned = normalize_space(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def slugify(value: str) -> str:
    value = normalize_space(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "record"


def clean_name(text: str) -> Optional[str]:
    text = normalize_space(text)
    if not text:
        return None
    if text.lower() in {x.lower() for x in SKIP_EXACT}:
        return None
    return text


def match_specialty(line: str) -> Optional[str]:
    line_clean = normalize_space(line)
    for canonical, aliases in SPECIALTY_ALIASES.items():
        if line_clean in aliases:
            return canonical
    return None


def match_region(line: str) -> Optional[str]:
    line_clean = normalize_space(line)
    return REGIONS.get(line_clean)


def looks_like_address(line: str) -> bool:
    lower = line.lower()
    if re.search(r"\b\d{5}\b", line):
        return False
    address_markers = [
        " rue ", " avenue ", " boulevard ", " bd ", " place ", " allée ", " route ",
        " square ", " impasse ", " passage ", " chemin ", " esplanade ", " quai ",
        " cours ", " cabinet ", " centre ", " clinique ", " hôpital", "hopital",
        "maison", "institut", "pole santé", "pôle santé", "cabinet", "msp", "groupe médical",
    ]
    if any(marker.strip() in lower for marker in address_markers):
        return True
    return bool(re.match(r"^\d+[A-Za-z\s'’.-]*$", line))


def looks_like_title(line: str) -> bool:
    lower = line.lower()
    title_markers = [
        "gynécologue", "gynéco", "médecin", "medecin", "psycho", "psychiatre",
        "sexologue", "sophrologue", "sage-femme", "sage femme", "ostéopathe",
        "radiologue", "algologue", "kiné", "kinés", "fertilité", "échographie",
        "hypnose", "réflexologue", "micronutrition", "dietet", "diétét",
    ]
    return any(marker in lower for marker in title_markers)


def looks_like_organization(line: str) -> bool:
    lower = line.lower()
    org_markers = [
        "cabinet", "centre", "clinique", "hôpital", "hopital", "institut", "maison",
        "groupe", "mutualiste", "croix-rouge", "pmi", "cpef", "association", "point gyn",
    ]
    return any(marker in lower for marker in org_markers)


def looks_like_booking(line: str) -> bool:
    lower = line.lower()
    return any(token in lower for token in ["doctolib", "maiia", "keldoc", "médoucine", "medoucine", "teleconsultation", "téléconsultation", "rdv"])


def extract_domains(line: str) -> List[str]:
    results = []
    for match in DOMAIN_RE.findall(line):
        value = match.strip(".,; ")
        if "@" in value:
            continue
        if not value.startswith("http"):
            value = "https://" + value
        results.append(value)
    return results


def clean_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("33") and len(digits) >= 11:
        digits = "0" + digits[-9:]
    if len(digits) == 10 and digits.startswith("0"):
        return " ".join([digits[:2], digits[2:4], digits[4:6], digits[6:8], digits[8:10]])
    return normalize_space(phone)


def is_new_name_line(line: str, current_specialty: Optional[str]) -> bool:
    line = normalize_space(line)
    if not line or any(ch.isdigit() for ch in line):
        return False
    if len(line) > 60:
        return False
    if looks_like_address(line) or looks_like_title(line) or looks_like_organization(line) or looks_like_booking(line):
        return False
    lower = line.lower()
    if any(keyword in lower for keyword in NON_NAME_KEYWORDS):
        return False
    words = re.split(r"[\s-]+", line)
    words = [w for w in words if w]
    if len(words) > 5 or len(words) < 2:
        return False
    letters = [c for c in line if c.isalpha()]
    uppercase_ratio = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0
    titlecase_ratio = sum(1 for w in words if w[:1].isupper()) / len(words)
    if uppercase_ratio >= 0.45 or titlecase_ratio >= 0.9:
        return True
    return False


def dedupe_records(records: List[ProfessionalRecord]) -> List[ProfessionalRecord]:
    seen = set()
    deduped = []
    for record in records:
        key = (
            (record.name or "").lower(),
            (record.specialty or "").lower(),
            (record.region or "").lower(),
            tuple(record.phones),
            tuple(record.address_lines),
            record.postal_code or "",
            (record.city or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def save_jsonl(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape the RESENDO health professionals directory.")
    parser.add_argument("--url", default=TARGET_URL, help="Directory URL to scrape.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output path. One JSON object is written per line.")
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay after the page request.")
    return parser


def main() -> None:
    parser = build_argparser()
    args = parser.parse_args()

    global TARGET_URL
    TARGET_URL = args.url

    scraper = ResendoScraper(sleep_seconds=args.sleep)
    rows = scraper.run()
    save_jsonl(Path(args.output), rows)
    print(f"Saved {len(rows)} professionals to {args.output}")


if __name__ == "__main__":
    main()
