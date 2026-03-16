import requests
from bs4 import BeautifulSoup
import urllib.parse

class Nyaa_search:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    
    def nyaafun(self, query):
        return self._search_site(query, "https://nyaa.si/")
    
    def nyaafap(self, query):
        return self._search_site(query, "https://sukebei.nyaa.si/")
    
    def _search_site(self, query, base_url):
        search_query = urllib.parse.quote_plus(query)
        url = f"{base_url}?q={search_query}&f=0&c=0_0&p=1"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            table = soup.find('table', class_='torrent-list')
            
            if not table:
                return []
            
            rows = table.find_all('tr')[1:]
            results = []
            
            for i, row in enumerate(rows, 1):
                try:
                    name_cell = row.find('td', colspan="2")
                    if not name_cell:
                        continue
                    
                    name_links = name_cell.find_all('a', href=lambda x: x and '/view/' in x)
                    if not name_links:
                        continue
                    
                    name = name_links[-1].get_text(strip=True)
                    
                    torrent_link = None
                    magnet_link = None
                    
                    download_links = row.find_all('a')
                    for link in download_links:
                        href = link.get('href', '')
                        if href.startswith('/download/'):
                            torrent_link = f"{base_url.rstrip('/')}{href}"
                        elif href.startswith('magnet:'):
                            magnet_link = href
                    
                    size_td = row.find('td', class_='text-center', string=lambda x: x and ('MiB' in x or 'GiB' in x))
                    size = size_td.get_text(strip=True) if size_td else "N/A"
                    
                    date_td = row.find('td', class_='text-center', attrs={'data-timestamp': True})
                    date = date_td.get_text(strip=True) if date_td else "N/A"
                    
                    results.append({
                        'result_number': i,
                        'name': name,
                        'torrent': torrent_link,
                        'magnet': magnet_link,
                        'size': size,
                        'date': date
                    })
                    
                except Exception:
                    continue
            
            return results
            
        except requests.RequestException:
            return []
        except Exception:
            return []