#!/usr/bin/env python3
"""
Endoccitanie.fr Detailed Profile Scraper
Scrapes complete practitioner data from individual profile pages
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import re
from urllib.parse import urljoin, urlparse
from pathlib import Path

# Configuration
BASE_URL = "https://www.endoccitanie.fr"
START_PAGE = 1
END_PAGE = 80
OUTPUT_FILE = "SRC029.json"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
    'Connection': 'keep-alive',
}

def get_page_url(page_num):
    """Generate URL for a specific page number"""
    return f"{BASE_URL}/annuaire/page/{page_num}/?geodir_search=1&stype=gd_place&s"

def clean_text(text):
    """Clean and normalize text"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text.strip())
    return text

def extract_practitioner_urls_from_listing(page_num, session):
    """Extract practitioner URLs from a listing page"""
    url = get_page_url(page_num)
    practitioners = []
    
    try:
        print(f"Scraping listing page {page_num}/80...")
        response = session.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find all practitioner cards
        cards = soup.find_all('div', class_='geodir-post')
        
        for card in cards:
            # Extract post ID
            post_id = card.get('data-post-id', '')
            
            # Find the link to the profile page
            link_elem = card.find('a', href=re.compile(r'/praticiens/[^/]+/?$'))
            if not link_elem:
                # Try finding any link with practitioner pattern
                link_elem = card.find('a', href=re.compile(r'/praticiens/'))
            
            if link_elem:
                profile_url = urljoin(BASE_URL, link_elem['href'])
                # Clean up URL - remove trailing slash and category links
                if '/category/' not in profile_url:
                    practitioners.append({
                        'id': post_id,
                        'profile_url': profile_url
                    })
        
        time.sleep(1)
        
    except Exception as e:
        print(f"Error on page {page_num}: {e}")
    
    return practitioners

def scrape_profile_page(practitioner, session):
    """Scrape detailed information from a profile page"""
    url = practitioner['profile_url']
    data = {
        "id": practitioner['id'],
        "name": "",
        "profession": "",
        "recours_label": "",
        "profile_url": url,
        "image": "",
        "specialites": [],
        "actes_pratiques": [],
        "lieu_exercice": {
            "structure": "",
            "address_lines": [],
            "postal_code": "",
            "city": ""
        },
        "appointment_info": "",
        "booking_links": []
    }
    
    try:
        print(f"  Scraping profile: {url}")
        response = session.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Extract Name - from title or h1
        title_elem = soup.find('h1') or soup.find('title')
        if title_elem:
            name = clean_text(title_elem.get_text())
            # Remove site name from title
            name = re.sub(r'\s*-\s*EndOccitanie\s*$', '', name)
            data['name'] = name
        
        # Alternative: find name in specific class
        if not data['name']:
            name_elem = soup.find(['h2', 'h3'], class_=re.compile('entry-title|post-title'))
            if name_elem:
                data['name'] = clean_text(name_elem.get_text())
        
        # Extract Profession - usually in a badge or category
        profession_elem = soup.find(['span', 'div'], class_=re.compile('profession|category|badge'))
        if profession_elem:
            data['profession'] = clean_text(profession_elem.get_text())
        
        # Try to find profession in content
        if not data['profession']:
            content = soup.get_text()
            professions = ['Sage femme', 'Médecin généraliste', 'Gynécologue', 'Endocrinologue', 
                          'Chirurgien', 'Kinésithérapeute', 'Ostéopathe', 'Psychologue', 
                          'Diététicien', 'Infirmier', 'Pédiatre', 'Urologue']
            for prof in professions:
                if prof.lower() in content.lower():
                    data['profession'] = prof
                    break
        
        # Extract Recours Label
        recours_elem = soup.find(['div', 'span', 'p'], class_=re.compile('recours'))
        if recours_elem:
            data['recours_label'] = clean_text(recours_elem.get_text())
        
        # Look for recours in text
        if not data['recours_label']:
            recours_match = re.search(r'(Recours\s+\d+\s*[-–]\s*[^<\n]+)', soup.get_text())
            if recours_match:
                data['recours_label'] = clean_text(recours_match.group(1))
        
        # Extract Image
        img_elem = soup.find('img', class_=re.compile('profile|avatar|praticien'))
        if not img_elem:
            img_elem = soup.find('img', src=re.compile(r'uploads.*\.(jpg|jpeg|png|webp)'))
        if img_elem:
            src = img_elem.get('data-src') or img_elem.get('src', '')
            if src:
                data['image'] = urljoin(BASE_URL, src)
        
        # Extract Specialites
        spec_section = soup.find(['div', 'section'], class_=re.compile('specialite|competence'))
        if spec_section:
            specs = spec_section.find_all(['li', 'span', 'div'], class_=re.compile('item|tag'))
            for spec in specs:
                text = clean_text(spec.get_text())
                if text and text not in data['specialites']:
                    data['specialites'].append(text)
        
        # Alternative: look for DIU mentions
        content = soup.get_text()
        diu_matches = re.findall(r'(DIU\s+[^,\n]+)', content)
        for match in diu_matches:
            match = clean_text(match)
            if match and match not in data['specialites']:
                data['specialites'].append(match)
        
        # Extract Actes Pratiques
        actes_section = soup.find(['div', 'section'], class_=re.compile('acte|soin|consultation'))
        if actes_section:
            actes = actes_section.find_all(['li', 'span', 'div'], class_=re.compile('item'))
            for acte in actes:
                text = clean_text(acte.get_text())
                if text and text not in data['actes_pratiques']:
                    data['actes_pratiques'].append(text)
        
        # Look for consultation mentions
        consult_matches = re.findall(r'(Consultation\s+[^,\n]+|Suivi\s+[^,\n]+)', content)
        for match in consult_matches:
            match = clean_text(match)
            if match and match not in data['actes_pratiques'] and len(match) < 100:
                data['actes_pratiques'].append(match)
        
        # Extract Lieu d'exercice (Address)
        address_elem = soup.find('address') or soup.find(['div', 'p'], class_=re.compile('address|adresse|location'))
        if address_elem:
            addr_text = clean_text(address_elem.get_text())
            lines = [l.strip() for l in addr_text.split('\n') if l.strip()]
            if lines:
                data['lieu_exercice']['address_lines'] = lines
        
        # Try to find address in specific format
        if not data['lieu_exercice']['address_lines']:
            # Look for postal code pattern
            postal_match = re.search(r'(\d{5})\s+([^\n,]+)', content)
            if postal_match:
                data['lieu_exercice']['postal_code'] = postal_match.group(1)
                data['lieu_exercice']['city'] = clean_text(postal_match.group(2))
        
        # Extract structure type
        structure_patterns = ['Cabinet libéral', 'Centre hospitalier', 'Clinique', 'Centre de santé']
        for pattern in structure_patterns:
            if pattern.lower() in content.lower():
                data['lieu_exercice']['structure'] = pattern
                break
        
        # Extract Appointment Info
        rdv_elem = soup.find(['div', 'p'], class_=re.compile('rdv|appointment|prise.*rendez'))
        if rdv_elem:
            data['appointment_info'] = clean_text(rdv_elem.get_text())
        
        # Look for appointment info in text
        if not data['appointment_info']:
            rdv_match = re.search(r'(Pour toutes prise de RDV[^<\n]+)', content)
            if rdv_match:
                data['appointment_info'] = clean_text(rdv_match.group(1))
        
        # Extract Booking Links (Doctolib, etc.)
        booking_links = soup.find_all('a', href=re.compile(r'doctolib|rdv|booking'))
        for link in booking_links:
            url = link.get('href', '')
            label = clean_text(link.get_text()) or 'Doctolib'
            if url and 'doctolib' in url:
                data['booking_links'].append({
                    'label': label,
                    'url': url
                })
        
        time.sleep(0.5)
        
    except Exception as e:
        print(f"    Error scraping profile {url}: {e}")
        data['error'] = str(e)
    
    return data

def main():
    """Main scraping function"""
    session = requests.Session()
    all_practitioners = []
    
    print("=" * 60)
    print("Endoccitanie.fr Detailed Profile Scraper")
    print("=" * 60)
    
    # Step 1: Collect all practitioner URLs from listing pages
    print("\nStep 1: Collecting practitioner URLs from listing pages...")
    practitioner_list = []
    
    for page_num in range(START_PAGE, END_PAGE + 1):
        page_practitioners = extract_practitioner_urls_from_listing(page_num, session)
        practitioner_list.extend(page_practitioners)
        
        if page_num % 10 == 0:
            print(f"  Progress: {page_num}/{END_PAGE} pages, {len(practitioner_list)} practitioners found")
    
    print(f"\nTotal practitioners found: {len(practitioner_list)}")
    
    # Remove duplicates based on profile_url
    seen_urls = set()
    unique_practitioners = []
    for p in practitioner_list:
        if p['profile_url'] not in seen_urls:
            seen_urls.add(p['profile_url'])
            unique_practitioners.append(p)
    
    print(f"Unique practitioners: {len(unique_practitioners)}")
    
    # Step 2: Scrape detailed information from each profile page
    print("\nStep 2: Scraping detailed profile information...")
    
    for i, practitioner in enumerate(unique_practitioners):
        print(f"\n[{i+1}/{len(unique_practitioners)}] Processing {practitioner['profile_url']}")
        detailed_data = scrape_profile_page(practitioner, session)
        all_practitioners.append(detailed_data)
        
        # Save progress every 10 practitioners
        if (i + 1) % 10 == 0:
            with open(OUTPUT_FILE + '.tmp', 'w', encoding='utf-8') as f:
                json.dump({
                    "metadata": {
                        "source": "endoccitanie.fr",
                        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "total_practitioners": len(all_practitioners),
                        "pages_scraped": END_PAGE
                    },
                    "practitioners": all_practitioners
                }, f, ensure_ascii=False, indent=2)
            print(f"  Progress saved: {i+1} profiles scraped")
    
    # Final save
    final_data = {
        "metadata": {
            "source": "endoccitanie.fr",
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_practitioners": len(all_practitioners),
            "pages_scraped": END_PAGE,
            "base_url": BASE_URL
        },
        "practitioners": all_practitioners
    }
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f"✓ Scraping complete!")
    print(f"✓ Total practitioners: {len(all_practitioners)}")
    print(f"✓ Output saved to: {OUTPUT_FILE}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()