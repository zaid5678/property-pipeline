"""
Gumtree UK — property for sale scraper.

Scrapes listings matching target areas and price range.
Filters to private sellers where possible (no "estate agent" in seller info).
Stores new listings in SQLite and returns them for alerting.
"""

import logging
import re
from urllib.parse import quote_plus

from scraper.base import fetch, url_hash, now_iso
from db.models import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.gumtree.com"


def _build_search_url(area: str, min_price: int, max_price: int, page: int = 1) -> str:
    slug = area.lower().strip().replace(" ", "-")
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


def _scrape_listing_detail(url: str, delay: float) -> dict:
    """Fetch individual listing page to get description and phone if visible."""
    soup = fetch(url, delay=delay)
    if not soup:
        return {}

    result = {}

    desc_el = soup.select_one(".ad-description")
    if desc_el:
        result["description"] = desc_el.get_text(" ", strip=True)[:2000]

    # Phone sometimes shown in seller info block
    seller_block = soup.select_one(".seller-phone, .phone-number, [data-q='seller-phone']")
    if seller_block:
        result["phone"] = _extract_phone(seller_block.get_text())

    if not result.get("phone") and result.get("description"):
        result["phone"] = _extract_phone(result["description"])

    return result


def _is_private_seller(card_html: str) -> bool:
    """Heuristic: if the card mentions estate agent keywords, skip it."""
    agent_keywords = [
        "estate agent", "letting agent", "property agent",
        "realtors", "rightmove", "zoopla", "purplebricks",
        "haart", "foxtons", "countrywide"
    ]
    low = card_html.lower()
    return not any(kw in low for kw in agent_keywords)


def scrape_area(area: str, min_price: int, max_price: int, delay: float = 3.0) -> list[dict]:
    """
    Scrape Gumtree listings for one area.
    Returns list of new (not-yet-seen) listing dicts.
    """
    new_listings = []
    conn = get_conn()

    for page in range(1, 4):  # scrape first 3 pages (≈90 listings)
        url = _build_search_url(area, min_price, max_price, page)
        logger.info("[Gumtree] Scraping %s page %d", area, page)
        soup = fetch(url, delay=delay)
        if not soup:
            break

        cards = soup.select("article.listing-maxi, li.listing-results-list-item")
        if not cards:
            # Try alternate selector
            cards = soup.select("[data-q='listing']")
        if not cards:
            logger.info("[Gumtree] No cards found on page %d for %s — stopping.", page, area)
            break

        for card in cards:
            try:
                listing = _parse_card(card, area)
            except Exception as exc:
                logger.debug("Card parse error: %s", exc)
                continue

            if not listing or not listing.get("url"):
                continue

            # Price filter
            price = listing.get("price")
            if price and (price < min_price or price > max_price):
                continue

            # Private seller filter
            if not _is_private_seller(str(card)):
                continue

            h = url_hash(listing["url"])
            existing = conn.execute(
                "SELECT id FROM properties WHERE url_hash = ?", (h,)
            ).fetchone()
            if existing:
                continue

            # Fetch detail page for description + phone
            detail = _scrape_listing_detail(listing["url"], delay)
            listing.update(detail)

            # Insert into DB
            listing["url_hash"] = h
            listing["scraped_at"] = now_iso()
            listing["source"] = "gumtree"

            try:
                conn.execute(
                    """INSERT INTO properties
                       (source, title, price, location, description, phone, url,
                        posted_date, scraped_at, url_hash)
                       VALUES (:source, :title, :price, :location, :description, :phone,
                               :url, :posted_date, :scraped_at, :url_hash)""",
                    {k: listing.get(k) for k in
                     ["source", "title", "price", "location", "description",
                      "phone", "url", "posted_date", "scraped_at", "url_hash"]}
                )
                conn.commit()
                new_listings.append(listing)
                logger.info("[Gumtree] NEW listing: %s — £%s", listing.get("title"), listing.get("price"))
            except Exception as exc:
                logger.debug("DB insert error: %s", exc)

    conn.close()
    return new_listings


def _parse_card(card, area: str) -> dict:
    listing = {}

    # Title
    title_el = card.select_one(
        "a.listing-link, .listing-title, h2.listing-title, [data-q='listing-title']"
    )
    if title_el:
        listing["title"] = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if href:
            listing["url"] = BASE_URL + href if href.startswith("/") else href

    # Price
    price_el = card.select_one(
        ".listing-price strong, .ad-price, [data-q='ad-price']"
    )
    if price_el:
        listing["price"] = _parse_price(price_el.get_text())

    # Location
    loc_el = card.select_one(
        ".listing-location, [data-q='listing-location']"
    )
    listing["location"] = loc_el.get_text(strip=True) if loc_el else area

    # Date
    date_el = card.select_one(
        ".listing-posted-date, [data-q='listing-date']"
    )
    listing["posted_date"] = date_el.get_text(strip=True) if date_el else None

    # Description snippet from card
    desc_el = card.select_one(
        ".listing-description, [data-q='listing-description']"
    )
    listing["description"] = desc_el.get_text(strip=True)[:500] if desc_el else None
    listing["phone"] = None

    return listing


def run_scraper(config: dict) -> list[dict]:
    """Entry point called by the scheduler. Returns all new listings found."""
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
            logger.error("[Gumtree] Error scraping %s: %s", area, exc)

    logger.info("[Gumtree] Run complete. %d new listings found.", len(all_new))
    return all_new
