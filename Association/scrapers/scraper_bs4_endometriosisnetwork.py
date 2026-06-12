#!/usr/bin/env python3
"""
Endometriosis Network Canada - Surgeon Directory Scraper
Uses BeautifulSoup to extract surgeon data from the directory page
"""

import json
import re
import requests
from bs4 import BeautifulSoup
from pathlib import Path


class EndometriosisNetworkScraper:
    """Scraper for the Endometriosis Network Canada Surgeon Directory"""
    
    BASE_URL = "https://endometriosisnetwork.com/surgeons/"
    API_ENDPOINT = "https://endometriosisnetwork.com/wp-admin/admin-ajax.php"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        self.surgeons = []
    
    def fetch_page(self, url=None, params=None):
        """Fetch a page from the website"""
        url = url or self.BASE_URL
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            print(f"❌ Error fetching {url}: {e}")
            return None
    
    def fetch_api_data(self, action, **kwargs):
        """Fetch data from the WordPress AJAX API"""
        params = {
            'action': action,
            'language': 'en',
            **kwargs
        }
        try:
            response = self.session.get(self.API_ENDPOINT, params=params, timeout=30)
            response.raise_for_status()
            return response.json() if response.text else None
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"❌ API error for action '{action}': {e}")
            return None
    
    def parse_surgeon_card(self, card_html):
        """Parse a single surgeon card HTML element"""
        soup = BeautifulSoup(str(card_html), 'html.parser')
        item = soup.find('li', class_='c-directory-sidebar__result')
        
        if not item:
            return None
        
        surgeon_data = {
            'doctor_id': None,
            'name': None,
            'hospitals': [],
            'locations': [],
            'referral_sources': [],
            'manages': [],
            'surgical_training': None,
            'languages': [],
            'dei_training': None,
            'survey_completed': None,
            'phone': None,
            'website': None,
            'additional_notes': []
        }
        
        # Get doctor ID from radio input
        radio_input = item.find('input', {'name': 'sidebar-result'})
        if radio_input:
            surgeon_data['doctor_id'] = radio_input.get('value')
        
        # Get name
        name_elem = item.find('span', class_='c-directory-card__title')
        if name_elem:
            surgeon_data['name'] = name_elem.get_text(strip=True)
        
        # Parse detail card
        detail_card = item.find('div', class_='c-directory-detail-card')
        if detail_card:
            # Hospital names
            hospital_elems = detail_card.find_all(
                'span', class_='c-directory-detail-card__address__location__name'
            )
            surgeon_data['hospitals'] = [
                h.get_text(strip=True) for h in hospital_elems
            ]
            
            # Locations (addresses)
            location_items = detail_card.find_all(
                'li', class_='c-directory-detail-card__address__location'
            )
            for loc in location_items:
                address_text = loc.get_text(separator=' ', strip=True)
                # Remove hospital name from address
                for hosp_name in surgeon_data['hospitals']:
                    address_text = address_text.replace(hosp_name, '')
                address_text = re.sub(r'\s+', ' ', address_text).strip()
                if address_text:
                    surgeon_data['locations'].append(address_text)
            
            # Meta information
            meta_items = detail_card.find_all(
                'li', class_='c-directory-detail-card__meta__item'
            )
            for meta in meta_items:
                key_elem = meta.find(
                    'span', class_='c-directory-detail-card__meta__item__key'
                )
                if key_elem:
                    key = key_elem.get_text(strip=True).rstrip(':')
                    # Get all text after the key
                    value = meta.get_text(strip=True).replace(key, '').strip(': ')
                    
                    self._categorize_meta_data(surgeon_data, key, value)
            
            # Phone number
            phone_link = detail_card.find('a', href=re.compile(r'tel:'))
            if phone_link:
                surgeon_data['phone'] = phone_link.get_text(strip=True)
            
            # Website
            website_link = detail_card.find(
                'a', href=re.compile(r'^https?://'), target='_blank'
            )
            if website_link:
                surgeon_data['website'] = website_link.get('href')
        
        return surgeon_data
    
    def _categorize_meta_data(self, data, key, value):
        """Categorize metadata into appropriate fields"""
        key_lower = key.lower()
        
        if 'referrals' in key_lower:
            data['referral_sources'] = [s.strip() for s in value.split(',')]
        elif 'manages' in key_lower:
            data['manages'] = [s.strip() for s in value.split(',')]
        elif 'surgical training' in key_lower:
            data['surgical_training'] = value
        elif 'languages' in key_lower:
            data['languages'] = [s.strip() for s in value.split(',')]
        elif 'diversity' in key_lower or 'dei' in key_lower or 'equity' in key_lower:
            data['dei_training'] = value
        elif 'survey' in key_lower:
            data['survey_completed'] = value
        elif 'practices out of' in key_lower or 'operates out of' in key_lower:
            data['additional_notes'].append(f"{key}: {value}")
    
    def scrape_from_html_file(self, filepath):
        """Scrape data from a local HTML file"""
        print(f"📁 Loading HTML from: {filepath}")
        
        with open(filepath, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        surgeon_items = soup.find_all('li', class_='c-directory-sidebar__result')
        
        print(f"🔍 Found {len(surgeon_items)} surgeon entries")
        
        for idx, item in enumerate(surgeon_items, 1):
            surgeon_data = self.parse_surgeon_card(item)
            if surgeon_data and surgeon_data['name']:
                surgeon_data['id'] = idx
                self.surgeons.append(surgeon_data)
                print(f"  ✓ {idx}. {surgeon_data['name']}")
        
        return self.surgeons
    
    def scrape_from_api(self, province='canada', limit=100):
        """
        Scrape data using the WordPress AJAX API
        Note: This may require additional parameters based on the site's API structure
        """
        print(f"🌐 Fetching surgeons from API (province: {province})...")
        
        # Try to fetch via the doctors pagination endpoint
        data = self.fetch_api_data(
            'doctors_pagination',
            limit=limit,
            format='json',
            taxonomies={'provinces': province}
        )
        
        if data and isinstance(data, dict):
            # Parse API response - structure may vary
            doctors = data.get('doctors', data.get('data', []))
            for idx, doctor in enumerate(doctors, 1):
                surgeon_data = self._parse_api_doctor(doctor, idx)
                if surgeon_data:
                    self.surgeons.append(surgeon_data)
        
        return self.surgeons
    
    def _parse_api_doctor(self, doctor_data, idx):
        """Parse doctor data from API response"""
        # This method should be adapted based on actual API response structure
        return {
            'id': idx,
            'doctor_id': doctor_data.get('id'),
            'name': doctor_data.get('title', {}).get('rendered'),
            'hospitals': doctor_data.get('hospitals', []),
            'locations': doctor_data.get('locations', []),
            'referral_sources': doctor_data.get('referral_sources', []),
            'manages': doctor_data.get('manages', []),
            'surgical_training': doctor_data.get('surgical_training'),
            'languages': doctor_data.get('languages', []),
            'dei_training': doctor_data.get('dei_training'),
            'survey_completed': doctor_data.get('survey_completed'),
            'phone': doctor_data.get('phone'),
            'website': doctor_data.get('website'),
            'additional_notes': doctor_data.get('additional_notes', [])
        }
    
    def scrape_from_web(self):
        """Scrape directly from the website"""
        print(f"🌐 Fetching {self.BASE_URL}...")
        
        html_content = self.fetch_page()
        if not html_content:
            return []
        
        soup = BeautifulSoup(html_content, 'html.parser')
        surgeon_items = soup.find_all('li', class_='c-directory-sidebar__result')
        
        print(f"🔍 Found {len(surgeon_items)} surgeon entries")
        
        for idx, item in enumerate(surgeon_items, 1):
            surgeon_data = self.parse_surgeon_card(item)
            if surgeon_data and surgeon_data['name']:
                surgeon_data['id'] = idx
                self.surgeons.append(surgeon_data)
                print(f"  ✓ {idx}. {surgeon_data['name']}")
        
        return self.surgeons
    
    def export_to_json(self, filepath='SRC20.json'):
        """Export scraped data to JSON file"""
        output_path = Path(filepath)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(self.surgeons, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 Exported {len(self.surgeons)} surgeons to: {output_path}")
        return output_path
    
    def export_to_csv(self, filepath='surgeons_data.csv'):
        """Export scraped data to CSV file"""
        import csv
        
        output_path = Path(filepath)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not self.surgeons:
            print("No data to export")
            return None
        
        # Flatten nested lists for CSV
        flat_data = []
        for s in self.surgeons:
            flat_s = {
                'id': s['id'],
                'doctor_id': s['doctor_id'],
                'name': s['name'],
                'hospitals': '; '.join(s['hospitals']),
                'locations': '; '.join(s['locations']),
                'referral_sources': '; '.join(s['referral_sources']),
                'manages': '; '.join(s['manages']),
                'surgical_training': s['surgical_training'],
                'languages': '; '.join(s['languages']),
                'dei_training': s['dei_training'],
                'survey_completed': s['survey_completed'],
                'phone': s['phone'],
                'website': s['website'],
                'additional_notes': '; '.join(s['additional_notes'])
            }
            flat_data.append(flat_s)
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=flat_data[0].keys())
            writer.writeheader()
            writer.writerows(flat_data)
        
        print(f"💾 Exported {len(flat_data)} surgeons to CSV: {output_path}")
        return output_path
    
    def get_statistics(self):
        """Get statistics about the scraped data"""
        if not self.surgeons:
            return "No data collected yet."
        
        stats = {
            'total_surgeons': len(self.surgeons),
            'with_phone': sum(1 for s in self.surgeons if s['phone']),
            'with_website': sum(1 for s in self.surgeons if s['website']),
            'with_training': sum(1 for s in self.surgeons if s['surgical_training']),
            'languages': set(),
            'provinces': set()
        }
        
        for s in self.surgeons:
            stats['languages'].update(s['languages'])
            # Extract province from locations
            for loc in s['locations']:
                if 'BC' in loc:
                    stats['provinces'].add('BC')
                elif 'ON' in loc:
                    stats['provinces'].add('ON')
                elif 'QC' in loc:
                    stats['provinces'].add('QC')
                elif 'AB' in loc:
                    stats['provinces'].add('AB')
                elif 'SK' in loc:
                    stats['provinces'].add('SK')
                elif 'MB' in loc:
                    stats['provinces'].add('MB')
                elif 'NS' in loc:
                    stats['provinces'].add('NS')
                elif 'NB' in loc:
                    stats['provinces'].add('NB')
                elif 'NL' in loc:
                    stats['provinces'].add('NL')
        
        return f"""
📊 Scraping Statistics:
======================
Total Surgeons: {stats['total_surgeons']}
With Phone: {stats['with_phone']}
With Website: {stats['with_website']}
With Surgical Training Info: {stats['with_training']}
Languages Found: {', '.join(sorted(stats['languages']))}
Provinces Found: {', '.join(sorted(stats['provinces']))}
"""


def main():
    """Main entry point - EDIT THIS PATH TO MATCH YOUR SYSTEM"""
    scraper = EndometriosisNetworkScraper()
    
    print("🔧 Endometriosis Network Canada - Surgeon Directory Scraper")
    print("=" * 60)
    
    # USE YOUR SPECIFIC PATH HERE
    file_path = r"E:\ZIWIG\Associations\Edometriosisnetwork\Surgeon.txt"
    
    scraper.scrape_from_html_file(file_path)
    
    if scraper.surgeons:
        print(scraper.get_statistics())
        
        # Export to same directory
        output_dir = Path(file_path).parent
        scraper.export_to_json(output_dir / 'SRC20.json')
        scraper.export_to_csv(output_dir / 'surgeons_data.csv')
    else:
        print("❌ No data could be extracted")


if __name__ == '__main__':
    main()