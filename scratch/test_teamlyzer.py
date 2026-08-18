import httpx
import bs4
import re

def main():
    res = httpx.get('https://pt.teamlyzer.com/', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, follow_redirects=True)
    print("Page status:", res.status_code)
    
    # Search for autocomplete or company data in HTML
    matches = re.findall(r'autocomplete|search_box|companies/', res.text)
    print("Keyword occurrences:", len(matches))
    
    # Check ranking list
    res_rank = httpx.get('https://pt.teamlyzer.com/companies/ranking', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, follow_redirects=True)
    soup_rank = bs4.BeautifulSoup(res_rank.text, 'html.parser')
    print("Ranking status:", res_rank.status_code)
    
    # Print sample ranking items
    for item in soup_rank.find_all('a', href=True):
        if '/companies/' in item['href'] and len(item['href'].split('/')) == 3:
            name = item.get_text(strip=True)
            if name and len(name) > 2:
                print(f"Company: {name} -> URL: {item['href']}")

if __name__ == '__main__':
    main()
