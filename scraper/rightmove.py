"""
Rightmove — for sale listings scraper.

Parses the window.jsonModel JSON blob embedded in search result pages.
Works from cloud IPs with proper headers.
"""

import json
import logging
import re
import time
import random
from urllib.parse import quote_plus

from scraper.base import fetch, url_hash, now_iso, SESSION
from db.models import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.rightmove.co.uk"

AUTOCOMPLETE_URL = (
    "https://www.rightmove.co.uk/typeAhead/uknoauth?"
    "input={query}&rent=false&sale=true"
)


# Hardcoded fallback IDs for common UK cities — used if the typeahead API fails.
_LOCATION_FALLBACK = {
    "birmingham":     "REGION^85168",
    "manchester":     "REGION^85191",
    "leeds":          "REGION^85208",
    "nottingham":     "REGION^85194",
    "sheffield":      "REGION^85203",
    "liverpool":      "REGION^85186",
    "bradford":       "REGION^85169",
    "leicester":      "REGION^85185",
    "london":         "REGION^87490",
    "bristol":        "REGION^85173",
    "coventry":       "REGION^85179",
    "derby":          "REGION^85180",
    "wolverhampton":  "REGION^85213",
    "stoke-on-trent": "REGION^85205",
    "hull":           "REGION^85183",
    "newcastle":      "REGION^85193",
    "sunderland":     "REGION^85206",
    "reading":        "REGION^85198",
    "edinburgh":      "REGION^85298",
    "glasgow":        "REGION^85304",
    "cardiff":        "REGION^87428",
}

_homepage_fetched = False


def _ensure_cookies() -> None:
    """Fetch Rightmove homepage once per session to obtain cookies.
    Without this the typeahead API returns an empty body."""
    global _homepage_fetched
    if _homepage_fetched:
        return
    try:
        SESSION.get("https://www.rightmove.co.uk/", timeout=15)
        _homepage_fetched = True
        logger.info("[Rightmove] Homepage fetched — cookies established")
    except Exception as exc:
        logger.warning("[Rightmove] Could not fetch homepage: %s", exc)


def _resolve_location_id(area: str, delay: float) -> str | None:
    _ensure_cookies()
    time.sleep(delay + random.uniform(0.3, 0.8))

    url = AUTOCOMPLETE_URL.format(query=quote_plus(area))
    try:
        resp = SESSION.get(
            url,
            timeout=15,
            headers={
                "Referer": "https://www.rightmove.co.uk/",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp.raise_for_status()
        if not resp.text.strip():
            raise ValueError("Empty response body")
        data = resp.json()
        results = data.get("typeAheadLocations", [])
        if results:
            loc_id = results[0].get("locationIdentifier", "")
            logger.info("[Rightmove] Resolved '%s' → %s", area, loc_id)
            return loc_id
        logger.warning("[Rightmove] No typeahead results for '%s'", area)
    except Exception as exc:
        logger.warning("[Rightmove] Typeahead failed for '%s': %s — trying fallback", area, exc)

    # Fallback: hardcoded location IDs
    slug = area.lower().strip()
    loc_id = _LOCATION_FALLBACK.get(slug)
    if loc_id:
        logger.info("[Rightmove] Using fallback location ID for '%s': %s", area, loc_id)
    else:
        logger.warning("[Rightmove] No fallback location ID for '%s' — skipping", area)
    return loc_id


def _build_search_url(location_id: str, min_price: int, max_price: int,
                      index: int = 0, keywords: str = "") -> str:
    url = (
        f"{BASE_URL}/property-for-sale/find.html"
        f"?locationIdentifier={quote_plus(location_id)}"
        f"&minPrice={min_price}"
        f"&maxPrice={max_price}"
        f"&index={index}"
        f"&includeSSTC=false"
        f"&sortType=6"  # newest first
    )
    if keywords:
        url += f"&keywords={quote_plus(keywords)}"
    return url


def _parse_price(val) -> int | None:
    try:
        return int(re.sub(r"[^\d]", "", str(val)))
    except (ValueError, TypeError):
        return None


def _extract_json_model(soup) -> list[dict]:
    """Extract property list from Rightmove's embedded window.jsonModel."""
    for tag in soup.find_all("script"):
        text = tag.string or ""
        if "jsonModel" not in text:
            continue
        match = re.search(r"window\.jsonModel\s*=\s*(\{.+?\})\s*;?\s*\n", text, re.DOTALL)
        if not match:
            # Try a more lenient pattern
            match = re.search(r"window\.jsonModel\s*=\s*(\{.*)", text, re.DOTALL)
            if match:
                # Trim to find the end of the object
                raw = match.group(1)
                # Find balanced braces
                depth, end = 0, 0
                for i, ch in enumerate(raw):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                raw = raw[:end]
            else:
                continue
        else:
            raw = match.group(1)

        try:
            model = json.loads(raw)
            props = model.get("properties", [])
            logger.info("[Rightmove] jsonModel found with %d properties", len(props))
            return props
        except json.JSONDecodeError as exc:
            logger.debug("[Rightmove] JSON parse error: %s", exc)
    return []


def _parse_html_cards(soup, area: str) -> list[dict]:
    """Fallback: parse HTML property cards."""
    listings = []
    cards = soup.select(
        "div.l-searchResult, [data-test='propertyCard'], "
        ".propertyCard, div[class*='propertyCard']"
    )
    logger.info("[Rightmove] HTML fallback found %d cards", len(cards))
    for card in cards:
        try:
            link = card.select_one("a[href*='/properties/']")
            if not link:
                continue
            href = link.get("href", "")
            url = BASE_URL + href if href.startswith("/") else href

            price_el = card.select_one(
                ".propertyCard-priceValue, [data-test='property-price'], "
                "[class*='price']"
            )
            addr_el = card.select_one(
                "address, .propertyCard-address, [data-test='address']"
            )
            desc_el = card.select_one(".propertyCard-description, [class*='description']")
            date_el = card.select_one(
                ".propertyCard-branchSummary-addedOrReduced, [class*='added']"
            )

            listings.append({
                "source": "rightmove",
                "title": addr_el.get_text(strip=True) if addr_el else area,
                "price": _parse_price(price_el.get_text()) if price_el else None,
                "location": addr_el.get_text(strip=True) if addr_el else area,
                "description": desc_el.get_text(strip=True)[:500] if desc_el else "",
                "phone": None,
                "posted_date": date_el.get_text(strip=True) if date_el else None,
                "url": url,
            })
        except Exception as exc:
            logger.debug("[Rightmove] Card parse error: %s", exc)
    return listings


def scrape_area(area: str, min_price: int, max_price: int,
                delay: float = 3.0, keywords: str = "") -> list[dict]:
    new_listings = []
    conn = get_conn()

    location_id = _resolve_location_id(area, delay)
    if not location_id:
        conn.close()
        return []

    for page_idx in [0, 24, 48]:
        url = _build_search_url(location_id, min_price, max_price, page_idx, keywords)
        logger.info("[Rightmove] Fetching %s", url)
        soup = fetch(url, delay=delay)
        if not soup:
            break

        # Diagnostic: log first 300 chars of page to catch blocks/redirects
        page_text = str(soup)[:300]
        logger.debug("[Rightmove] Page snippet: %s", page_text)

        # Try JSON model first, then HTML fallback
        raw_props = _extract_json_model(soup)
        if raw_props:
            page_listings = _props_from_json(raw_props, area)
        else:
            logger.info("[Rightmove] No jsonModel, trying HTML cards")
            page_listings = _parse_html_cards(soup, area)

        if not page_listings:
            logger.info("[Rightmove] No listings on page index %d for %s", page_idx, area)
            break

        for listing in page_listings:
            if not listing.get("url"):
                continue
            price = listing.get("price")
            if price and (price < min_price or price > max_price):
                continue

            h = url_hash(listing["url"])
            if conn.execute("SELECT id FROM properties WHERE url_hash=?", (h,)).fetchone():
                continue

            listing.update({"url_hash": h, "scraped_at": now_iso(), "source": "rightmove"})
            try:
                conn.execute(
                    """INSERT INTO properties
                       (source,title,price,location,description,phone,url,
                        posted_date,scraped_at,url_hash)
                       VALUES (:source,:title,:price,:location,:description,:phone,
                               :url,:posted_date,:scraped_at,:url_hash)""",
                    {k: listing.get(k) for k in
                     ["source","title","price","location","description","phone",
                      "url","posted_date","scraped_at","url_hash"]}
                )
                conn.commit()
                new_listings.append(listing)
                logger.info("[Rightmove] NEW: %s — £%s", listing.get("title"), listing.get("price"))
            except Exception as exc:
                logger.debug("DB insert error: %s", exc)

    conn.close()
    return new_listings


def _props_from_json(props: list, area: str) -> list[dict]:
    listings = []
    for prop in props:
        try:
            price_info = prop.get("price", {})
            amount = price_info.get("amount") or price_info.get("displayPrices", [{}])[0].get("displayPrice", "")
            href = prop.get("propertyUrl", "")
            listings.append({
                "source": "rightmove",
                "title": prop.get("displayAddress") or prop.get("summary", area),
                "price": _parse_price(amount),
                "location": prop.get("displayAddress", area),
                "description": prop.get("summary", ""),
                "phone": None,
                "posted_date": prop.get("firstVisibleDate") or prop.get("addedOrReduced"),
                "url": BASE_URL + href if href.startswith("/") else href,
            })
        except Exception as exc:
            logger.debug("[Rightmove] JSON prop parse error: %s", exc)
    return listings


def run_scraper(config: dict) -> list[dict]:
    all_new = []
    areas = config["scraper"]["target_areas"]
    min_p = config["scraper"]["price"]["min"]
    max_p = config["scraper"]["price"]["max"]
    delay = config["scraper"].get("request_delay", 3)

    keyword_searches = [
        "refurbishment",
        "renovation",
        "no chain",
        "cash buyer",
        "probate",
        "reduced",
    ]

    for area in areas:
        # General search
        try:
            all_new.extend(scrape_area(area, min_p, max_p, delay))
        except Exception as exc:
            logger.error("[Rightmove] Error scraping %s: %s", area, exc)

        # Keyword-targeted searches (Rightmove supports one keyword at a time)
        for kw in keyword_searches:
            try:
                all_new.extend(scrape_area(area, min_p, max_p, delay, keywords=kw))
            except Exception as exc:
                logger.error("[Rightmove] Error scraping %s keyword '%s': %s", area, kw, exc)

    logger.info("[Rightmove] Done. %d new listings.", len(all_new))
    return all_new
