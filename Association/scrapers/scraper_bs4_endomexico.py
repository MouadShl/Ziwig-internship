import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_URL = "https://endometriosismexico.com/index.php/directorio-medico/"
OUTPUT_FILE = Path("SRC034.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

phone_re = re.compile(r'(?:Citas?|Tel(?:\.|éfono)?|WhatsApp|Inbox)\s*:?\s*(.+)', re.I)
email_re = re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)
url_re = re.compile(r'^(?:https?://|www\.)', re.I)
postal_re = re.compile(r'\b\d{5}\b')


def clean(text: str) -> str:
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' |\n\t\r')


def extract_lines_from_p(p: Tag):
    lines = []
    buf = []
    for child in p.children:
        if isinstance(child, NavigableString):
            txt = clean(str(child))
            if txt:
                buf.append(txt)
        elif isinstance(child, Tag):
            if child.name == 'br':
                joined = clean(' '.join(buf))
                if joined:
                    lines.append(joined)
                buf = []
            else:
                txt = clean(child.get_text(' ', strip=True))
                if txt:
                    buf.append(txt)
    joined = clean(' '.join(buf))
    if joined:
        lines.append(joined)
    return [ln for ln in lines if ln and not ln.startswith('<!--')]


def is_new_entry(p: Tag, lines):
    if not lines:
        return False
    if p.find(['strong', 'b']):
        return True
    first = lines[0]
    if first.lower().startswith(('dr.', 'dra.', 'lic.', 'psict.', 'mtra.')):
        return True
    if first.startswith('NutriADN') or first.startswith('CT Scanner') or first.startswith('Psicoterapeuta'):
        return True
    return False


def split_fields(lines, links):
    name = lines[0] if lines else ''
    specialty_lines = []
    address_lines = []
    notes = []
    phones = []
    emails = []
    websites = []
    booking_links = []

    for href, label in links:
        if href:
            full = href if href.startswith('http') else urljoin(BASE_URL, href)
            websites.append(full)
            if 'calendly' in full.lower() or 'facebook.com' in full.lower():
                booking_links.append(full)

    for ln in lines[1:]:
        m = phone_re.search(ln)
        found_email = email_re.findall(ln)
        if found_email:
            emails.extend(found_email)
        if m:
            phones.append(clean(m.group(1)))
            prefix = clean(ln[:m.start()])
            if prefix:
                notes.append(prefix)
            continue
        if url_re.match(ln):
            websites.append(ln if ln.startswith('http') else f'https://{ln}')
            continue
        if 'Calendly' in ln or 'Sitio web:' in ln:
            notes.append(ln)
            continue
        if ln.startswith('*'):
            notes.append(ln)
            continue
        if any(k in ln.lower() for k in ['consulta', 'hospital', 'consultorio', 'col.', 'colonia', 'av.', 'blvd.', 'boulevard', 'calle', 'piso', 'torre', 'cp ', 'c.p.', 'ciudad de méxico', 'zapopan', 'monterrey', 'morelia', 'tijuana', 'querétaro', 'aguascalientes', 'celaya', 'guadalajara', 'polanco', 'miguel hidalgo', 'san pedro garza garcía', 'edo de mexico']):
            address_lines.append(ln)
        else:
            specialty_lines.append(ln)

    flat_addr = ' | '.join(address_lines)
    postal_codes = postal_re.findall(flat_addr)
    return {
        'name': name,
        'specialties': specialty_lines,
        'addresses': address_lines,
        'phones': phones,
        'emails': sorted(dict.fromkeys(emails)),
        'websites': sorted(dict.fromkeys(websites)),
        'booking_links': sorted(dict.fromkeys(booking_links)),
        'notes': notes,
        'postal_codes': sorted(dict.fromkeys(postal_codes)),
    }


def scrape_directory():
    response = requests.get(BASE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('article .blog-post-entry')
    if not content:
        raise RuntimeError('Could not find the main directory content block.')

    city = None
    entries = []
    current = None

    for child in content.children:
        if not isinstance(child, Tag):
            continue
        if child.name == 'h4':
            city = clean(child.get_text(' ', strip=True))
            continue
        if child.name != 'p':
            continue
        lines = extract_lines_from_p(child)
        if not lines:
            continue
        links = [(a.get('href'), clean(a.get_text(' ', strip=True))) for a in child.find_all('a', href=True)]
        if is_new_entry(child, lines):
            current = {'city_section': city, 'raw_lines': lines[:], 'links': links[:]}
            entries.append(current)
        elif current is not None:
            current['raw_lines'].extend(lines)
            current['links'].extend(links)

    records = []
    for i, entry in enumerate(entries, start=1):
        fields = split_fields(entry['raw_lines'], entry['links'])
        rec = {
            'id': f'SRC034_{i:03d}',
            'city_section': entry['city_section'],
            'name': fields['name'],
            'specialties': fields['specialties'],
            'addresses': fields['addresses'],
            'postal_codes': fields['postal_codes'],
            'phones': fields['phones'],
            'emails': fields['emails'],
            'websites': fields['websites'],
            'booking_links': fields['booking_links'],
            'notes': fields['notes'],
            'source_url': BASE_URL,
            'raw_lines': entry['raw_lines'],
        }
        records.append(rec)

    payload = {
        'source_id': 'SRC034',
        'source_url': BASE_URL,
        'title': clean(soup.title.get_text(' ', strip=True)) if soup.title else 'Directorio Médico',
        'total_entries': len(records),
        'cities': sorted(dict.fromkeys(r['city_section'] for r in records if r['city_section'])),
        'entries': records,
    }
    return payload


def main():
    data = scrape_directory()
    with OUTPUT_FILE.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Scraped {data['total_entries']} entries across {len(data['cities'])} cities.")
    print(f"Saved to {OUTPUT_FILE.resolve()}")


if __name__ == '__main__':
    main()
