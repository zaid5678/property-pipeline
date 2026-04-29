"""
OnTheMarket — for sale listings scraper.

OnTheMarket uses Next.js and embeds all property data in a
<script id="__NEXT_DATA__"> JSON blob on the page — no JS execution needed,
no Cloudflare, works reliably from cloud IPs.
"""

import json
import logging
import re
from urllib.parse import quote_plus

from scraper.base import fetch, url_hash, now_iso
from db.models import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.onthemarket.com"


def _area_slug(area: str) -> str:
    return area.lower().strip().replace(" ", "-")


def _build_search_url(area: str, min_price: int, max_price: int, page: int = 1) -> str:
    slug = _area_slug(area)
    params = f"min-price={min_price}&max-price={max_price}"
    if page > 1:
        params += f"&page={page}"
    return f"{BASE_URL}/for-sale/property/{slug}/?{params}"


def _parse_price(val) -> int | None:
    try:
        return int(re.sub(r"[^\d]", "", str(val)))
    except (ValueError, TypeError):
        return None


def _extract_phone(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"(\+44\s?|0)[\d\s\-]{9,13}", text)
    return match.group(0).strip() if match else None


def _parse_next_data(soup) -> list[dict]:
    """Extract properties from Next.js __NEXT_DATA__ JSON blob."""
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag or not tag.string:
        logger.warning("[OTM] No __NEXT_DATA__ found")
        return []

    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError as exc:
        logger.warning("[OTM] JSON parse error: %s", exc)
        return []

    # Properties live at different paths depending on OTM's page version
    page_props = data.get("props", {}).get("pageProps", {})

    # Try known paths
    for key in ("properties", "listings", "results", "data"):
        if key in page_props and isinstance(page_props[key], list):
            logger.info("[OTM] Found %d properties under pageProps.%s", len(page_props[key]), key)
            return page_props[key]

    # Recurse one level deeper
    for val in page_props.values():
        if isinstance(val, dict):
            for key in ("properties", "listings", "results"):
                if key in val and isinstance(val[key], list):
                    logger.info("[OTM] Found %d properties under nested key '%s'", len(val[key]), key)
                    return val[key]

    logger.warning("[OTM] Could not find property list in __NEXT_DATA__. Keys: %s",
                   list(page_props.keys()))
    logger.debug("[OTM] __NEXT_DATA__ snippet: %s", tag.string[:500])
    return []


def _parse_html_fallback(soup, area: str) -> list[dict]:
    """Fallback HTML parser for OTM listing cards."""
    listings = []
    cards = soup.select(
        "li[data-testid='listing-card'], "
        "div[data-testid='listing-card'], "
        ".property-card, article[class*='property']"
    )
    logger.info("[OTM] HTML fallback: %d cards found", len(cards))
    for card in cards:
        try:
            link = card.select_one("a[href*='/details/']")
            if not link:
                continue
            href = link.get("href", "")
            url = BASE_URL + href if href.startswith("/") else href

            price_el = card.select_one(
                "[data-testid='price'], .price, [class*='price']"
            )
            addr_el = card.select_one(
                "[data-testid='address'], address, [class*='address']"
            )
            desc_el = card.select_one(
                "[data-testid='description'], [class*='description'], p"
            )

            listings.append({
                "title": addr_el.get_text(strip=True) if addr_el else area,
                "price": _parse_price(price_el.get_text()) if price_el else None,
                "location": addr_el.get_text(strip=True) if addr_el else area,
                "description": desc_el.get_text(strip=True)[:500] if desc_el else "",
                "phone": None,
                "posted_date": None,
                "url": url,
            })
        except Exception as exc:
            logger.debug("[OTM] Card parse error: %s", exc)
    return listings


def _listing_from_json(prop: dict, area: str) -> dict | None:
    """Convert an OTM JSON property object to our standard listing dict."""
    try:
        # OTM uses various shapes — handle both known versions
        price_raw = (
            prop.get("price")
            or prop.get("pricing", {}).get("price")
            or prop.get("listingPriceDisplay")
            or ""
        )
        address = (
            prop.get("address")
            or prop.get("displayAddress")
            or prop.get("location", {}).get("address")
            or area
        )
        if isinstance(address, dict):
            address = ", ".join(filter(None, [
                address.get("line1"), address.get("line2"),
                address.get("town"), address.get("postcode")
            ]))

        summary = (
            prop.get("summary")
            or prop.get("description")
            or prop.get("shortDescription")
            or ""
        )

        slug = (
            prop.get("detailUrl")
            or prop.get("propertyUrl")
            or prop.get("id")
            or ""
        )
        if slug and not slug.startswith("http"):
            url = BASE_URL + slug if slug.startswith("/") else f"{BASE_URL}/details/{slug}/"
        else:
            url = slug

        if not url:
            return None

        phone_raw = (
            prop.get("phone")
            or prop.get("contactPhone")
            or prop.get("agent", {}).get("phone")
            or ""
        )

        return {
            "title": address,
            "price": _parse_price(price_raw),
            "location": address,
            "description": str(summary)[:500],
            "phone": _extract_phone(str(phone_raw)) if phone_raw else None,
            "posted_date": prop.get("addedOn") or prop.get("dateAdded"),
            "url": url,
        }
    except Exception as exc:
        logger.debug("[OTM] JSON prop parse error: %s", exc)
        return None


def scrape_area(area: str, min_price: int, max_price: int, delay: float = 3.0) -> list[dict]:
    new_listings = []
    conn = get_conn()

    for page in range(1, 4):
        url = _build_search_url(area, min_price, max_price, page)
        logger.info("[OTM] Scraping %s page %d — %s", area, page, url)
        soup = fetch(url, delay=delay)
        if not soup:
            break

        # Try __NEXT_DATA__ first
        raw_props = _parse_next_data(soup)
        if raw_props:
            page_listings = [
                l for p in raw_props
                if (l := _listing_from_json(p, area)) is not None
            ]
        else:
            page_listings = _parse_html_fallback(soup, area)

        logger.info("[OTM] %s page %d: %d listings parsed", area, page, len(page_listings))

        if not page_listings:
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

            listing.update({"url_hash": h, "scraped_at": now_iso(), "source": "onthemarket"})
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
                logger.info("[OTM] NEW: %s — £%s", listing.get("title"), listing.get("price"))
            except Exception as exc:
                logger.debug("[OTM] DB insert error: %s", exc)

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
            logger.error("[OTM] Error scraping %s: %s", area, exc)

    logger.info("[OTM] Done. %d new listings.", len(all_new))
    return all_new
