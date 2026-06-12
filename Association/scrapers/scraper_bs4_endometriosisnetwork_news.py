#!/usr/bin/env python3
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
    text = text.replace("\xa0", " ").replace("\ufeff", "")
    text = re.sub(r"\r", "", text)
    text = re.sub(r"\s+", " ", text)
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
    return clean_text(cleaned.get_text('\n', strip=True))


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
        r'(?:last updated|updated)[:\s-]+([A-Za-z]+\s+\d{1,2},\s*\d{4})',
        r'(?:last updated|updated)[:\s-]+(\d{4}-\d{2}-\d{2})',
        r'(?:last updated|updated)[:\s-]+(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
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
