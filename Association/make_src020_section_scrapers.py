from pathlib import Path
from textwrap import dedent

OUT = Path('.')

COMMON = '''#!/usr/bin/env python3
import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session(base_url: str):
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-CA,en;q=0.9",
        "Referer": base_url.rstrip('/') + '/',
    })
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


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\\xa0", " ").replace("\\ufeff", "")
    text = re.sub(r"\\r", "", text)
    text = re.sub(r"\\s+", " ", text)
    return text.strip()


def normalize_url(base_url: str, href: str) -> str:
    if not href:
        return ""
    full = urljoin(base_url.rstrip('/') + '/', href)
    parsed = urlparse(full)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return full


def clean_soup(soup: BeautifulSoup) -> BeautifulSoup:
    soup = BeautifulSoup(str(soup), 'lxml')
    for sel in [
        'script', 'style', 'noscript', 'svg', 'iframe', 'form', 'nav',
        'header', 'footer', '.menu', '.navigation', '.breadcrumbs',
        '.breadcrumb', '.pagination', '.pager', '.newsletter', '.cookie',
        '.announcement-bar', '.subscription', '.share', '.social'
    ]:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    return soup


def extract_main_container(soup: BeautifulSoup):
    for sel in ['main', 'article', '[role="main"]', '.entry-content', '.node__content', '.post-content', '.content', 'body']:
        el = soup.select_one(sel)
        if el:
            return el
    return soup


def extract_title(soup: BeautifulSoup) -> str:
    for sel in ['h1', 'main h1', 'article h1', 'title']:
        el = soup.select_one(sel)
        if el:
            return clean_text(el.get_text(' ', strip=True))
    return ''


def extract_text(container) -> str:
    cleaned = clean_soup(container)
    return clean_text(cleaned.get_text('\\n', strip=True))


def extract_headings(container, level: str):
    return [clean_text(x.get_text(' ', strip=True)) for x in container.select(level) if clean_text(x.get_text(' ', strip=True))]


def extract_bullets(container):
    groups = []
    for ul in container.select('ul, ol'):
        items = [clean_text(li.get_text(' ', strip=True)) for li in ul.select('li') if clean_text(li.get_text(' ', strip=True))]
        if items:
            groups.append(items)
    return groups


def extract_pdfs(container, page_url: str):
    out = []
    seen = set()
    for a in container.select('a[href]'):
        href = normalize_url(page_url, a.get('href', ''))
        if href and '.pdf' in href.lower() and href not in seen:
            seen.add(href)
            out.append({'text': clean_text(a.get_text(' ', strip=True)), 'url': href})
    return out


def extract_links(container, page_url: str):
    out = []
    seen = set()
    for a in container.select('a[href]'):
        href = normalize_url(page_url, a.get('href', ''))
        text = clean_text(a.get_text(' ', strip=True))
        if href and href not in seen:
            seen.add(href)
            out.append({'text': text, 'url': href})
    return out


def infer_language(soup):
    html_tag = soup.select_one('html')
    lang = clean_text(html_tag.get('lang', '')) if html_tag else ''
    return 'FR' if lang.lower().startswith('fr') else 'EN'


def extract_last_updated(text: str) -> str:
    patterns = [
        r'(?:last updated|updated)[:\\s-]+([A-Za-z]+\\s+\\d{1,2},\\s*\\d{4})',
        r'(?:last updated|updated)[:\\s-]+(\\d{4}-\\d{2}-\\d{2})',
        r'(?:last updated|updated)[:\\s-]+(\\d{1,2}\\s+[A-Za-z]+\\s+\\d{4})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return clean_text(m.group(1))
    return ''


def fetch_html(session, url: str, sleep_seconds: float = 1.0) -> str:
    time.sleep(sleep_seconds)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def ensure_output_dir(source_id: str):
    output_dir = Path('outputs') / source_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def load_config(config_path: str):
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)
'''

FILES = {
    'scraper_bs4_endometriosisnetwork_endo_hub.py': COMMON + '''

class EndoHubScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_endo_hub_content.json"
        self.seed_urls = [
            '/what-is-endometriosis/',
            '/endo-symptoms/',
            '/paths-to-diagnosis/',
            '/management-options/',
            '/endo-hub/surgery-a-guide-for-people-with-endometriosis/',
        ]

    def parse_page(self, url: str):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        container = extract_main_container(soup)
        text = extract_text(container)
        faq_sections = []
        for h in container.select('h2, h3, h4, details summary'):
            q = clean_text(h.get_text(' ', strip=True))
            if '?' not in q and not q.lower().startswith('faq'):
                continue
            answer_parts = []
            sib = h
            steps = 0
            while steps < 10:
                sib = sib.find_next_sibling()
                if sib is None or getattr(sib, 'name', None) in {'h2', 'h3', 'h4', 'summary'}:
                    break
                if getattr(sib, 'name', None) in {'p', 'div', 'section', 'ul', 'ol'}:
                    val = clean_text(sib.get_text(' ', strip=True))
                    if val:
                        answer_parts.append(val)
                steps += 1
            if answer_parts:
                faq_sections.append({'question': q, 'answer': ' '.join(answer_parts)})
        french_link = ''
        for a in soup.select('a[href]'):
            href = a.get('href', '')
            text_a = clean_text(a.get_text(' ', strip=True)).lower()
            if 'reseaudelendometriose.com' in href or text_a in {'fr', 'français'}:
                french_link = href
                break
        return {
            'source_id': self.source_id,
            'page_title': extract_title(soup),
            'url': url,
            'main_content': text,
            'headings_h2': extract_headings(container, 'h2'),
            'headings_h3': extract_headings(container, 'h3'),
            'downloadable_pdfs': extract_pdfs(container, url),
            'bullet_lists': extract_bullets(container),
            'faq_sections': faq_sections,
            'last_updated_date': extract_last_updated(text),
            'language': infer_language(soup),
            'french_version_url': french_link,
            'all_links': extract_links(container, url),
        }

    def run(self):
        rows = []
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                rows.append(self.parse_page(url))
                print(f'saved {url}')
            except Exception as e:
                rows.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    EndoHubScraper(args.config).run()
''',
    'scraper_bs4_endometriosisnetwork_support.py': COMMON + '''

class SupportGroupsScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_support_groups.json"
        self.seed_urls = ['/support/', '/virtual-support-groups/', '/support-group-registration/']

    def parse_groups_from_page(self, soup, url):
        container = extract_main_container(soup)
        rows = []
        for h in container.select('h2, h3'):
            title = clean_text(h.get_text(' ', strip=True))
            if not title or title.lower() in {'related content', 'registration form', 'how to register'}:
                continue
            block_texts, join_links = [], []
            nxt, steps = h, 0
            while steps < 12:
                nxt = nxt.find_next_sibling()
                if nxt is None or getattr(nxt, 'name', None) in {'h2', 'h3'}:
                    break
                txt = clean_text(nxt.get_text(' ', strip=True))
                if txt:
                    block_texts.append(txt)
                for a in nxt.select('a[href]'):
                    href = normalize_url(url, a.get('href', ''))
                    label = clean_text(a.get_text(' ', strip=True))
                    if href:
                        join_links.append({'text': label, 'url': href})
                steps += 1
            blob = ' '.join(block_texts)
            if 'support' in title.lower() or 'group' in title.lower() or 'facilitator' in blob.lower():
                rows.append({
                    'source_id': self.source_id,
                    'group_name': title,
                    'meeting_schedule': self.extract_schedule(blob),
                    'how_to_join': join_links,
                    'facilitator_name': self.extract_facilitator(blob),
                    'description': blob,
                    'target_audience': self.extract_target_audience(blob),
                    'language': 'FR' if 'français' in blob.lower() or 'french' in blob.lower() else infer_language(soup),
                    'source_url': url,
                })
        return rows

    def extract_schedule(self, text):
        m = re.findall(r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[^\\.]{0,80}(\\d{1,2}(?::\\d{2})?\\s*(?:a\\.m\\.|p\\.m\\.|am|pm)?)', text, re.I)
        return [' '.join(x) for x in m]

    def extract_facilitator(self, text):
        m = re.search(r'([A-Z][a-z]+\\s+[A-Z][a-z]+)\\s*\\([^)]*\\)\\s+is\\s+a\\s+Support Group Facilitator', text)
        return m.group(1) if m else ''

    def extract_target_audience(self, text):
        labels = []
        for keyword in ['general', 'youth', 'parents', 'caregivers', 'province', 'canada', 'french', 'english']:
            if keyword in text.lower():
                labels.append(keyword)
        return labels

    def run(self):
        records, seen = [], set()
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
                page_rows = self.parse_groups_from_page(soup, url)
                for row in page_rows:
                    key = (row['group_name'].lower(), row['source_url'])
                    if key not in seen:
                        seen.add(key)
                        records.append(row)
                container = extract_main_container(soup)
                for link in extract_links(container, url):
                    u = link['url'].lower()
                    if any(x in u for x in ['facebook.com', 'discord', 'slack']):
                        records.append({
                            'source_id': self.source_id,
                            'group_name': link['text'] or 'Community Link',
                            'meeting_schedule': [],
                            'how_to_join': [link],
                            'facilitator_name': '',
                            'description': '',
                            'target_audience': ['community'],
                            'language': infer_language(soup),
                            'source_url': url,
                        })
                print(f'saved {url}')
            except Exception as e:
                records.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    SupportGroupsScraper(args.config).run()
''',
    'scraper_bs4_endometriosisnetwork_events.py': COMMON + '''

class EventsScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_events.json"
        self.seed_urls = [
            '/ways-to-help/',
            '/ways-to-help/?category=attend-an-event',
            '/events/',
            '/ways-to-help/run-to-end-endo/',
            '/ways-to-help/the-endo-networks-run-to-end-endo-2026/',
            '/ways-to-help/illuminations-light-up-for-endo-2026/',
        ]

    def discover_event_links(self, soup, page_url):
        links, seen = [], set()
        for a in soup.select('a[href]'):
            href = normalize_url(page_url, a.get('href', ''))
            txt = clean_text(a.get_text(' ', strip=True))
            blob = f'{txt} {href}'.lower()
            if href and any(x in blob for x in ['event', 'run-to-end-endo', 'webinar', 'illuminations', 'wellness']):
                if href not in seen:
                    seen.add(href)
                    links.append(href)
        return links

    def parse_event_page(self, url):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        container = extract_main_container(soup)
        text = extract_text(container)
        registration_links = [x for x in extract_links(container, url) if any(k in x['text'].lower() + ' ' + x['url'].lower() for k in ['register', 'ticket', 'eventbrite', 'raceroster', 'zoom'])]
        event_date = ''
        m = re.search(r'([A-Z][a-z]+\\s+\\d{1,2}(?:st|nd|rd|th)?[,]?\\s+20\\d{2})', text)
        if m:
            event_date = clean_text(m.group(1))
        event_type = []
        low = text.lower()
        for label in ['virtual', 'in-person', 'fundraiser', 'webinar', 'annual', 'monthly']:
            if label in low:
                event_type.append(label)
        location = ''
        m2 = re.search(r'(?:location|where)[:\\s-]+([^\\.]{3,120})', text, re.I)
        if m2:
            location = clean_text(m2.group(1))
        is_recurring = 'annual' in low or 'monthly' in low or 'every year' in low
        return {
            'source_id': self.source_id,
            'event_name': extract_title(soup),
            'event_date': event_date,
            'event_type': event_type,
            'location': location,
            'registration_link': registration_links,
            'description': text,
            'is_recurring': is_recurring,
            'url': url,
            'pdf_links': extract_pdfs(container, url),
        }

    def run(self):
        discovered, rows = set(), []
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
                discovered.add(url)
                for link in self.discover_event_links(soup, url):
                    discovered.add(link)
            except Exception:
                pass
        for url in sorted(discovered):
            try:
                rows.append(self.parse_event_page(url))
                print(f'saved {url}')
            except Exception as e:
                rows.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    EventsScraper(args.config).run()
''',
    'scraper_bs4_endometriosisnetwork_period_program.py': COMMON + '''

class PeriodProgramScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_period_program.json"
        self.seed_urls = [
            '/endo-hub/what-you-need-to-know-period/',
            '/endo-hub/facilitator-training-what-you-need-to-know-period/',
            '/endo-hub/',
            '/about/programs/',
        ]

    def parse_page(self, url):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        container = extract_main_container(soup)
        text = extract_text(container)
        partner_orgs = []
        for pattern in ['Health Canada', 'CanSAGE', 'school', 'community', 'educator', 'teacher']:
            if pattern.lower() in text.lower():
                partner_orgs.append(pattern)
        contacts = [x for x in extract_links(container, url) if 'mailto:' in x['url'] or 'contact' in x['text'].lower()]
        return {
            'source_id': self.source_id,
            'url': url,
            'page_title': extract_title(soup),
            'program_overview': text,
            'target_schools_regions': [x for x in ['school', 'youth', 'community', 'educator'] if x in text.lower()],
            'downloadable_curriculum': extract_pdfs(container, url),
            'partner_organizations': partner_orgs,
            'contact_for_educators': contacts,
            'headings_h2': extract_headings(container, 'h2'),
            'headings_h3': extract_headings(container, 'h3'),
            'language': infer_language(soup),
        }

    def run(self):
        rows = []
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                rows.append(self.parse_page(url))
                print(f'saved {url}')
            except Exception as e:
                rows.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    PeriodProgramScraper(args.config).run()
''',
    'scraper_bs4_endometriosisnetwork_impact.py': COMMON + '''

class ImpactReportsScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_impact_reports.json"
        self.seed_urls = ['/impact-reports/', '/our-story/', '/about-the-endometriosis-network/']

    def extract_stats(self, text):
        stats = []
        for m in re.finditer(r'([^\\.\\n]{0,60}(?:\\$\\d+[\\d\\.,]*|\\d+[\\d\\.,]*%|\\d+[\\d\\.,]*\\+?)[^\\.\\n]{0,120})', text):
            snippet = clean_text(m.group(1))
            if len(snippet) >= 8:
                stats.append(snippet)
        return list(dict.fromkeys(stats))[:100]

    def parse_page(self, url):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        container = extract_main_container(soup)
        text = extract_text(container)
        pdfs = extract_pdfs(container, url)
        report_years = sorted(set(re.findall(r'20\\d{2}', ' '.join([extract_title(soup), text] + [x['url'] for x in pdfs]))))
        partners = [x for x in ['CanSAGE', 'Health Canada', 'community', 'partner', 'media'] if x.lower() in text.lower()]
        board_members = []
        for h in container.select('h2, h3, h4'):
            title = clean_text(h.get_text(' ', strip=True)).lower()
            if 'board' in title or 'team' in title:
                sib, steps = h, 0
                while steps < 20:
                    sib = sib.find_next_sibling()
                    if sib is None or getattr(sib, 'name', None) in {'h2', 'h3', 'h4'}:
                        break
                    val = clean_text(sib.get_text(' ', strip=True))
                    if val and len(val.split()) <= 12:
                        board_members.append(val)
                    steps += 1
        return {
            'source_id': self.source_id,
            'url': url,
            'page_title': extract_title(soup),
            'report_year': report_years,
            'key_statistics': self.extract_stats(text),
            'program_highlights': extract_headings(container, 'h2') + extract_headings(container, 'h3'),
            'funding_info': [s for s in self.extract_stats(text) if '$' in s or 'fund' in s.lower() or 'grant' in s.lower()],
            'media_coverage': [x for x in extract_links(container, url) if any(k in x['text'].lower() + ' ' + x['url'].lower() for k in ['media', 'press', 'news'])],
            'board_members': list(dict.fromkeys(board_members))[:50],
            'partnerships': partners,
            'pdf_links': pdfs,
            'main_content': text,
        }

    def run(self):
        rows = []
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                rows.append(self.parse_page(url))
                print(f'saved {url}')
            except Exception as e:
                rows.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    ImpactReportsScraper(args.config).run()
''',
    'scraper_bs4_endometriosisnetwork_other_directories.py': COMMON + '''

class OtherDirectoriesScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_other_directories.json"
        self.seed_urls = ['/clinics/', '/resources/', '/directory/', '/external-support-and-resources/', '/endo-hub/']

    def parse_resource_like_page(self, url):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        container = extract_main_container(soup)
        links = extract_links(container, url)
        records = []
        for link in links:
            blob = (link['text'] + ' ' + link['url']).lower()
            if any(k in blob for k in ['clinic', 'directory', 'resource', 'support', 'organization', 'hospital']):
                records.append({
                    'source_id': self.source_id,
                    'resource_name': link['text'],
                    'type': self.classify(link),
                    'location_province': '',
                    'contact_info': [link] if 'mailto:' in link['url'] or 'tel:' in link['url'] else [],
                    'services_offered': [],
                    'specializations': [],
                    'source_url': url,
                    'resource_url': link['url'],
                })
        return records

    def classify(self, link):
        blob = (link['text'] + ' ' + link['url']).lower()
        for label in ['clinic', 'organization', 'support', 'resource', 'directory', 'hospital']:
            if label in blob:
                return label.title()
        return 'Resource'

    def run(self):
        rows, seen = [], set()
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                for row in self.parse_resource_like_page(url):
                    key = (row['resource_name'].lower(), row['resource_url'])
                    if key not in seen:
                        seen.add(key)
                        rows.append(row)
                print(f'saved {url}')
            except Exception as e:
                rows.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    OtherDirectoriesScraper(args.config).run()
''',
    'scraper_bs4_endometriosisnetwork_news.py': COMMON + '''

class NewsBlogScraper:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.base_url = self.config['base_url']
        self.source_id = self.config['source_id']
        self.session = build_session(self.base_url)
        self.sleep_seconds = float(self.config.get('sleep_seconds', 1.0))
        self.output_path = ensure_output_dir(self.source_id) / f"{self.source_id}_news_blog.json"
        self.seed_urls = ['/endo-hub/', '/news/', '/blog/']

    def discover_article_links(self, url):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        links, seen = [], set()
        for a in soup.select('a[href]'):
            href = normalize_url(url, a.get('href', ''))
            if not href:
                continue
            path = urlparse(href).path.rstrip('/')
            if path.startswith('/endo-hub/') and path != '/endo-hub':
                if href not in seen:
                    seen.add(href)
                    links.append(href)
            elif any(path.startswith(p) for p in ['/news/', '/blog/']):
                if href not in seen:
                    seen.add(href)
                    links.append(href)
        return links

    def parse_article(self, url):
        soup = BeautifulSoup(fetch_html(self.session, url, self.sleep_seconds), 'lxml')
        container = extract_main_container(soup)
        text = extract_text(container)
        title = extract_title(soup)
        publish_date = extract_last_updated(text)
        featured = ''
        img = container.select_one('img')
        if img:
            featured = normalize_url(url, img.get('src') or img.get('data-src') or '')
        tags = []
        low = text.lower()
        for tag in ['news', 'research', 'story', 'resource', 'checklist', 'guide', 'support']:
            if tag in low or tag in title.lower():
                tags.append(tag)
        category = tags[0].title() if tags else 'Article'
        return {
            'source_id': self.source_id,
            'title': title,
            'publish_date': publish_date,
            'author': '',
            'content': text,
            'category': category,
            'tags': tags,
            'featured_image_url': featured,
            'url': url,
            'pdf_links': extract_pdfs(container, url),
        }

    def run(self):
        links = set()
        for href in self.seed_urls:
            url = normalize_url(self.base_url, href)
            try:
                links.update(self.discover_article_links(url))
            except Exception:
                pass
        rows = []
        for url in sorted(links):
            try:
                rows.append(self.parse_article(url))
                print(f'saved {url}')
            except Exception as e:
                rows.append({'source_id': self.source_id, 'url': url, 'error': str(e)})
                print(f'failed {url}: {e}')
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(self.output_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    NewsBlogScraper(args.config).run()
''',
}

for name, content in FILES.items():
    (OUT / name).write_text(dedent(content), encoding='utf-8')

print('Wrote files:')
for name in FILES:
    print(name)
