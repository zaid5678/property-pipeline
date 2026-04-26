"""
Rightmove — reduced price for sale listings scraper.

Searches Rightmove for properties with "reduced" in the description/badge,
within target areas and price range.  Rightmove is server-side rendered for
most listing data, so BeautifulSoup works for the key fields.
"""

import json
import logging
import re
from urllib.parse import quote_plus

from scraper.base import fetch, url_hash, now_iso
from db.models import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.rightmove.co.uk"

# Rightmove location identifiers must be looked up.  We use their
# autocomplete endpoint to resolve a town name to a region/outcode ID.
AUTOCOMPLETE_URL = (
    "https://www.rightmove.co.uk/typeAhead/uknoauth?"
    "input={query}&rent=false&sale=true"
)


def _resolve_location_id(area: str, delay: float) -> str | None:
    """Use Rightmove's typeahead API to get a locationIdentifier for a town."""
    import requests
    from scraper.base import SESSION
    import time, random

    time.sleep(delay + random.uniform(0.3, 1.0))
    url = AUTOCOMPLETE_URL.format(query=quote_plus(area))
    try:
        resp = SESSION.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # First result is usually the best match
        results = data.get("typeAheadLocations", [])
        if results:
            loc_id = results[0].get("locationIdentifier", "")
            logger.debug("[Rightmove] Resolved '%s' → %s", area, loc_id)
            return loc_id
    except Exception as exc:
        logger.warning("[Rightmove] Location lookup failed for '%s': %s", area, exc)
    return None


def _build_search_url(location_id: str, min_price: int, max_price: int, index: int = 0) -> str:
    return (
        f"{BASE_URL}/property-for-sale/find.html"
        f"?locationIdentifier={quote_plus(location_id)}"
        f"&minPrice={min_price}"
        f"&maxPrice={max_price}"
        f"&mustHave=priceReduced"
        f"&index={index}"
        f"&_includeSSTC=false"
    )


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _scrape_page(soup, area: str) -> list[dict]:
    listings = []

    # Rightmove embeds property data in a JSON blob: window.jsonModel
    script_tags = soup.find_all("script")
    json_model = None
    for tag in script_tags:
        text = tag.string or ""
        if "jsonModel" in text:
            match = re.search(r"window\.jsonModel\s*=\s*(\{.*?\});", text, re.DOTALL)
            if match:
                try:
                    json_model = json.loads(match.group(1))
                    break
                except json.JSONDecodeError:
                    pass

    if json_model:
        properties = json_model.get("properties", [])
        for prop in properties:
            try:
                price_info = prop.get("price", {})
                listing = {
                    "source": "rightmove",
                    "title": prop.get("summary", prop.get("displayAddress", "")),
                    "price": _parse_price(str(price_info.get("amount", ""))),
                    "location": prop.get("displayAddress", area),
                    "description": prop.get("summary", ""),
                    "phone": None,
                    "posted_date": prop.get("firstVisibleDate"),
                    "url": BASE_URL + prop.get("propertyUrl", ""),
                }
                listings.append(listing)
            except Exception as exc:
                logger.debug("JSON prop parse error: %s", exc)
        return listings

    # Fallback: parse HTML cards
    cards = soup.select("div.l-searchResult, [data-test='propertyCard']")
    for card in cards:
        try:
            listing = _parse_html_card(card, area)
            if listing:
                listings.append(listing)
        except Exception as exc:
            logger.debug("HTML card parse error: %s", exc)

    return listings


def _parse_html_card(card, area: str) -> dict | None:
    title_el = card.select_one("h2.propertyCard-title, [data-test='property-header']")
    price_el = card.select_one(".propertyCard-priceValue, [data-test='property-price']")
    addr_el = card.select_one("address.propertyCard-address")
    link_el = card.select_one("a.propertyCard-link, a[data-test='property-details']")
    desc_el = card.select_one(".propertyCard-description")
    date_el = card.select_one(".propertyCard-branchSummary-addedOrReduced")

    if not link_el:
        return None

    href = link_el.get("href", "")
    url = BASE_URL + href if href.startswith("/") else href

    return {
        "source": "rightmove",
        "title": title_el.get_text(strip=True) if title_el else "",
        "price": _parse_price(price_el.get_text()) if price_el else None,
        "location": addr_el.get_text(strip=True) if addr_el else area,
        "description": desc_el.get_text(strip=True)[:500] if desc_el else "",
        "phone": None,
        "posted_date": date_el.get_text(strip=True) if date_el else None,
        "url": url,
    }


def scrape_area(area: str, min_price: int, max_price: int, delay: float = 3.0) -> list[dict]:
    """Scrape Rightmove reduced listings for one area."""
    new_listings = []
    conn = get_conn()

    location_id = _resolve_location_id(area, delay)
    if not location_id:
        logger.warning("[Rightmove] Could not resolve location for '%s', skipping.", area)
        conn.close()
        return []

    for page_idx in [0, 24, 48]:  # pages 1–3 (24 results per page)
        url = _build_search_url(location_id, min_price, max_price, page_idx)
        logger.info("[Rightmove] Scraping %s (index %d)", area, page_idx)
        soup = fetch(url, delay=delay)
        if not soup:
            break

        page_listings = _scrape_page(soup, area)
        if not page_listings:
            break

        for listing in page_listings:
            if not listing.get("url"):
                continue

            price = listing.get("price")
            if price and (price < min_price or price > max_price):
                continue

            h = url_hash(listing["url"])
            existing = conn.execute(
                "SELECT id FROM properties WHERE url_hash = ?", (h,)
            ).fetchone()
            if existing:
                continue

            listing["url_hash"] = h
            listing["scraped_at"] = now_iso()

            try:
                conn.execute(
                    """INSERT INTO properties
                       (source, title, price, location, description, phone, url,
                        posted_date, scraped_at, url_hash)
                       VALUES (:source, :title, :price, :location, :description,
                               :phone, :url, :posted_date, :scraped_at, :url_hash)""",
                    {k: listing.get(k) for k in
                     ["source", "title", "price", "location", "description",
                      "phone", "url", "posted_date", "scraped_at", "url_hash"]}
                )
                conn.commit()
                new_listings.append(listing)
                logger.info("[Rightmove] NEW listing: %s — £%s",
                            listing.get("title"), listing.get("price"))
            except Exception as exc:
                logger.debug("DB insert error: %s", exc)

    conn.close()
    return new_listings


def run_scraper(config: dict) -> list[dict]:
    """Entry point called by the scheduler."""
    all_new = []
    areas = config["scraper"]["target_areas"]
    min_p = config["scraper"]["price"]["min"]
    max_p = config["scraper"]["price"]["max"]
    delay = config["scraper"].get("request_delay", 3)

    for area in areas:
        try:
            new = scrape_area(area, min_p, max_p, delay)
            all_new.extend(new)
        except Exception as exc:
            logger.error("[Rightmove] Error scraping %s: %s", area, exc)

    logger.info("[Rightmove] Run complete. %d new listings found.", len(all_new))
    return all_new
