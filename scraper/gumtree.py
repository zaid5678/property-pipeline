"""
Gumtree UK scraper.

NOTE: Gumtree is protected by Cloudflare and blocks all datacenter IPs
(including GitHub Actions). This scraper works when run locally but will
return 0 results on cloud runners. OnTheMarket is used as the cloud-friendly
replacement. This file is kept for local use only.
"""

import logging
import re
from urllib.parse import quote_plus

from scraper.base import fetch, url_hash, now_iso
from db.models import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.gumtree.com"


def _build_search_url(area: str, min_price: int, max_price: int, page: int = 1) -> str:
    params = (
        f"search_category=property-for-sale"
        f"&search_location={quote_plus(area)}"
        f"&min_price={min_price}"
        f"&max_price={max_price}"
        f"&page={page}"
    )
    return f"{BASE_URL}/search?{params}"


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_phone(text: str) -> str | None:
    match = re.search(r"(\+44\s?|0)[\d\s\-]{9,13}", text)
    return match.group(0).strip() if match else None


def _is_private_seller(card_html: str) -> bool:
    agent_keywords = [
        "estate agent", "letting agent", "property agent",
        "purplebricks", "haart", "foxtons", "countrywide",
    ]
    low = card_html.lower()
    return not any(kw in low for kw in agent_keywords)


def _parse_card(card, area: str) -> dict:
    listing = {}

    title_el = card.select_one(
        "a.listing-link, .listing-title, h2.listing-title, [data-q='listing-title'], "
        ".natural .title, a[href*='/property-for-sale/']"
    )
    if title_el:
        listing["title"] = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if href:
            listing["url"] = BASE_URL + href if href.startswith("/") else href

    price_el = card.select_one(
        ".listing-price strong, .ad-price, [data-q='ad-price'], .price"
    )
    if price_el:
        listing["price"] = _parse_price(price_el.get_text())

    loc_el = card.select_one(".listing-location, [data-q='listing-location'], .location")
    listing["location"] = loc_el.get_text(strip=True) if loc_el else area

    date_el = card.select_one(".listing-posted-date, [data-q='listing-date']")
    listing["posted_date"] = date_el.get_text(strip=True) if date_el else None

    desc_el = card.select_one(".listing-description, [data-q='listing-description']")
    listing["description"] = desc_el.get_text(strip=True)[:500] if desc_el else None
    listing["phone"] = None

    return listing


def scrape_area(area: str, min_price: int, max_price: int, delay: float = 3.0) -> list[dict]:
    new_listings = []
    conn = get_conn()

    for page in range(1, 4):
        url = _build_search_url(area, min_price, max_price, page)
        logger.info("[Gumtree] Scraping %s page %d", area, page)
        soup = fetch(url, delay=delay)
        if not soup:
            logger.warning("[Gumtree] No response for %s page %d", area, page)
            break

        # Cloudflare challenge detection
        page_text = soup.get_text()
        if "cf-browser-verification" in str(soup) or "Just a moment" in page_text:
            logger.warning(
                "[Gumtree] Cloudflare block detected for %s — "
                "this scraper only works from residential IPs, not cloud runners.", area
            )
            break

        cards = soup.select(
            "article.listing-maxi, li.listing-results-list-item, "
            "[data-q='listing'], .natural"
        )
        logger.info("[Gumtree] Found %d cards on page %d for %s", len(cards), page, area)

        if not cards:
            logger.debug("[Gumtree] HTML snippet: %s", str(soup)[:500])
            break

        for card in cards:
            try:
                listing = _parse_card(card, area)
            except Exception as exc:
                logger.debug("Card parse error: %s", exc)
                continue

            if not listing or not listing.get("url"):
                continue
            if not _is_private_seller(str(card)):
                continue

            price = listing.get("price")
            if price and (price < min_price or price > max_price):
                continue

            h = url_hash(listing["url"])
            if conn.execute("SELECT id FROM properties WHERE url_hash=?", (h,)).fetchone():
                continue

            listing.update({"url_hash": h, "scraped_at": now_iso(), "source": "gumtree"})
            try:
                conn.execute(
                    """INSERT INTO properties
                       (source,title,price,location,description,phone,url,posted_date,scraped_at,url_hash)
                       VALUES (:source,:title,:price,:location,:description,:phone,
                               :url,:posted_date,:scraped_at,:url_hash)""",
                    {k: listing.get(k) for k in
                     ["source","title","price","location","description","phone",
                      "url","posted_date","scraped_at","url_hash"]}
                )
                conn.commit()
                new_listings.append(listing)
                logger.info("[Gumtree] NEW: %s — £%s", listing.get("title"), listing.get("price"))
            except Exception as exc:
                logger.debug("DB insert error: %s", exc)

    conn.close()
    return new_listings


def run_scraper(config: dict) -> list[dict]:
    all_new = []
    areas = config["scraper"]["target_areas"]
    min_p = config["scraper"]["price"]["min"]
    max_p = config["scraper"]["price"]["max"]
    delay = config["scraper"].get("request_delay", 3)

    for area in areas:
        try:
            all_new.extend(scrape_area(area, min_p, max_p, delay))
        except Exception as exc:
            logger.error("[Gumtree] Error scraping %s: %s", area, exc)

    logger.info("[Gumtree] Done. %d new listings.", len(all_new))
    return all_new
