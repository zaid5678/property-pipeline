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

        # Check for __NEXT_DATA__
        nd = soup.find("script", {"id": "__NEXT_DATA__"})
        if nd and nd.string:
            print(f"__NEXT_DATA__ found — {len(nd.string)} chars")
            try:
                data = json.loads(nd.string)
                page_props = data.get("props", {}).get("pageProps", {})
                print(f"pageProps keys: {list(page_props.keys())}")

                # Recursively find any list with >3 items (likely the property list)
                def find_lists(obj, path=""):
                    if isinstance(obj, list) and len(obj) > 3:
                        print(f"  LIST at '{path}': {len(obj)} items, first keys: {list(obj[0].keys()) if obj and isinstance(obj[0], dict) else 'non-dict'}")
                    elif isinstance(obj, dict):
                        for k, v in obj.items():
                            find_lists(v, f"{path}.{k}")

                find_lists(page_props, "pageProps")
            except Exception as e:
                print(f"JSON parse error: {e}")
                print(f"__NEXT_DATA__ snippet: {nd.string[:300]}")
        else:
            print("NO __NEXT_DATA__ found")
            print(f"Page title: {soup.title.string if soup.title else 'no title'}")
            print(f"First 500 chars of body: {soup.get_text()[:500]}")

        # Count any listing-shaped elements
        for sel in ["li[data-testid]", "div[data-testid]", "[class*='property']",
                    "[class*='listing']", "article"]:
            els = soup.select(sel)
            if els:
                print(f"Selector '{sel}': {len(els)} elements")

    except Exception as e:
        print(f"ERROR: {e}")


def check_rightmove():
    print(f"\n{SEP}\nRIGHTMOVE — location lookup\n{SEP}")

    # Step 1: resolve location ID
    turl = "https://www.rightmove.co.uk/typeAhead/uknoauth?input=Birmingham&rent=false&sale=true"
    print(f"Typeahead URL: {turl}")
    loc_id = None
    try:
        time.sleep(2)
        r = SESSION.get(turl, timeout=15)
        print(f"Status: {r.status_code}")
        data = r.json()
        print(f"Response: {json.dumps(data)[:400]}")
        results = data.get("typeAheadLocations", [])
        if results:
            loc_id = results[0].get("locationIdentifier")
            print(f"Location ID: {loc_id}")
        else:
            print("No locations returned")
    except Exception as e:
        print(f"Typeahead ERROR: {e}")

    if not loc_id:
        print("Skipping search — no location ID")
        return

    print(f"\n{SEP}\nRIGHTMOVE — search\n{SEP}")
    from urllib.parse import quote_plus
    url = (
        f"https://www.rightmove.co.uk/property-for-sale/find.html"
        f"?locationIdentifier={quote_plus(loc_id)}"
        f"&minPrice=50000&maxPrice=250000&index=0&includeSSTC=false&sortType=6"
    )
    print(f"URL: {url}")
    try:
        time.sleep(3)
        r = SESSION.get(url, timeout=20)
        print(f"Status: {r.status_code}  |  Size: {len(r.text)} chars")

        soup = BeautifulSoup(r.text, "lxml")
        print(f"Page title: {soup.title.string if soup.title else 'no title'}")

        # Check for jsonModel
        jm_found = False
        for tag in soup.find_all("script"):
            text = tag.string or ""
            if "jsonModel" in text:
                jm_found = True
                print(f"jsonModel script found ({len(text)} chars)")
                match = re.search(r'"properties"\s*:\s*(\[)', text)
                if match:
                    print("'properties' array found inside jsonModel")
                else:
                    print("NO 'properties' array in jsonModel")
                    print(f"jsonModel snippet: {text[text.find('jsonModel'):text.find('jsonModel')+200]}")
                break
        if not jm_found:
            print("NO jsonModel found in any script tag")
            print(f"Script tags: {len(soup.find_all('script'))}")
            print(f"First 500 chars: {r.text[:500]}")

        # HTML card check
        for sel in ["div.l-searchResult", "[data-test='propertyCard']",
                    ".propertyCard", "[class*='propertyCard']"]:
            els = soup.select(sel)
            if els:
                print(f"HTML selector '{sel}': {len(els)} cards")

    except Exception as e:
        print(f"Search ERROR: {e}")


if __name__ == "__main__":
    check_onthemarket()
    check_rightmove()
    print(f"\n{SEP}\nDone\n{SEP}")
