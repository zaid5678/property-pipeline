"""
Diagnostic script — run this to see exactly what each site returns.
Output appears in the GitHub Actions log.

Usage: python debug_scrapers.py
"""

import json
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SEP = "=" * 60


def check_onthemarket():
    print(f"\n{SEP}\nONTHEMARKET\n{SEP}")
    url = "https://www.onthemarket.com/for-sale/property/birmingham/?min-price=50000&max-price=250000"
    print(f"URL: {url}")
    try:
        r = SESSION.get(url, timeout=20)
        print(f"Status: {r.status_code}  |  Size: {len(r.text)} chars")

        soup = BeautifulSoup(r.text, "lxml")
        articles = soup.select("article")
        print(f"Article elements: {len(articles)}")

        if articles:
            print(f"\n--- First article HTML (first 1500 chars) ---")
            print(str(articles[0])[:1500])
            print("---")

            # Try to find prices
            for i, art in enumerate(articles[:3]):
                price_match = re.search(r"£([\d,]+)", art.get_text())
                links = [a.get("href","") for a in art.find_all("a", href=True)]
                print(f"Article {i+1}: price={'£'+price_match.group(1) if price_match else 'none'} | links={links[:2]}")

    except Exception as e:
        print(f"ERROR: {e}")


def check_rightmove():
    print(f"\n{SEP}\nRIGHTMOVE — fetching homepage for cookies\n{SEP}")
    try:
        r = SESSION.get("https://www.rightmove.co.uk/", timeout=15)
        print(f"Homepage status: {r.status_code} | cookies: {list(SESSION.cookies.keys())}")
    except Exception as e:
        print(f"Homepage ERROR: {e}")

    print(f"\n{SEP}\nRIGHTMOVE — typeahead\n{SEP}")
    turl = "https://www.rightmove.co.uk/typeAhead/uknoauth?input=Birmingham&rent=false&sale=true"
    print(f"URL: {turl}")
    try:
        time.sleep(2)
        r = SESSION.get(
            turl, timeout=15,
            headers={
                "Referer": "https://www.rightmove.co.uk/",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        print(f"Status: {r.status_code} | Body length: {len(r.text)}")
        print(f"Raw response: {repr(r.text[:300])}")

        if r.text.strip():
            data = r.json()
            results = data.get("typeAheadLocations", [])
            print(f"Locations returned: {len(results)}")
            if results:
                print(f"First result: {results[0]}")
                loc_id = results[0].get("locationIdentifier")

                # Test a search with this ID
                from urllib.parse import quote_plus
                search_url = (
                    f"https://www.rightmove.co.uk/property-for-sale/find.html"
                    f"?locationIdentifier={quote_plus(loc_id)}"
                    f"&minPrice=50000&maxPrice=250000&index=0&includeSSTC=false&sortType=6"
                )
                print(f"\n{SEP}\nRIGHTMOVE — search with ID {loc_id}\n{SEP}")
                print(f"URL: {search_url}")
                time.sleep(3)
                sr = SESSION.get(search_url, timeout=20)
                print(f"Status: {sr.status_code} | Size: {len(sr.text)}")
                ssoup = BeautifulSoup(sr.text, "lxml")
                has_jm = any("jsonModel" in (t.string or "") for t in ssoup.find_all("script"))
                print(f"Has jsonModel: {has_jm}")
                if has_jm:
                    for t in ssoup.find_all("script"):
                        if "jsonModel" in (t.string or ""):
                            m = re.search(r'"properties"\s*:\s*\[', t.string)
                            count = len(re.findall(r'"propertyUrl"', t.string or ""))
                            print(f"'properties' array found: {bool(m)} | propertyUrl count: {count}")
        else:
            print("Empty body — trying fallback location ID")
            from urllib.parse import quote_plus
            loc_id = "REGION^85168"  # Birmingham fallback
            search_url = (
                f"https://www.rightmove.co.uk/property-for-sale/find.html"
                f"?locationIdentifier={quote_plus(loc_id)}"
                f"&minPrice=50000&maxPrice=250000&index=0&includeSSTC=false&sortType=6"
            )
            print(f"Search URL with fallback ID: {search_url}")
            time.sleep(3)
            sr = SESSION.get(search_url, timeout=20)
            print(f"Status: {sr.status_code} | Size: {len(sr.text)}")
            ssoup = BeautifulSoup(sr.text, "lxml")
            has_jm = any("jsonModel" in (t.string or "") for t in ssoup.find_all("script"))
            count = 0
            for t in ssoup.find_all("script"):
                count += len(re.findall(r'"propertyUrl"', t.string or ""))
            print(f"Has jsonModel: {has_jm} | propertyUrl occurrences: {count}")

    except Exception as e:
        import traceback
        print(f"ERROR: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    check_onthemarket()
    check_rightmove()
    print(f"\n{SEP}\nDone\n{SEP}")
