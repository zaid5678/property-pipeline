"""
OnTheMarket — for sale listings scraper.

Parses the 30 article elements on each search results page.
Works reliably from cloud IPs.
"""

import json
import logging
import re
from urllib.parse import quote_plus

from scraper.base import fetch, url_hash, now_iso
from db.models import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.onthemarket.com"


def _build_search_url(area: str, min_price: int, max_price: int,
                      page: int = 1, keywords: str = "") -> str:
    slug = area.lower().strip().replace(" ", "-")
    params = f"min-price={min_price}&max-price={max_price}"
    if keywords:
        params += f"&keywords={quote_plus(keywords)}"
    if page > 1:
        params += f"&page={page}"
    return f"{BASE_URL}/for-sale/property/{slug}/?{params}"


def _parse_price(text: str) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_phone(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"(\+44\s?|0)[\d\s\-]{9,13}", text)
    return match.group(0).strip() if match else None


def _find_price_in_article(article) -> int | None:
    """Find the first £X,XXX-style price anywhere in the article."""
    for el in article.find_all(True):
        text = el.get_text(strip=True)
        m = re.search(r"£([\d,]+)", text)
        if m:
            val = _parse_price(m.group(1))
            if val and val > 10000:   # ignore small numbers like £300/m²
                return val
    return None


def _parse_article(article, area: str) -> dict | None:
    """Extract listing data from a single <article> element."""
    # URL — must have a details link
    link = article.find("a", href=re.compile(r"/details/\d+"))
    if not link:
        # Broader fallback
        link = article.find("a", href=True)
    if not link:
        return None

    href = link.get("href", "")
    url = BASE_URL + href if href.startswith("/") else href
    if not url or "onthemarket.com" not in url and not href.startswith("/"):
        return None

    # Price
    price = _find_price_in_article(article)

    # Address / title — try headings first, then any prominent text element
    address = ""
    for tag in ["h2", "h3", "h4", "[data-testid*='address']", "[class*='address']",
                "[class*='Address']", "[class*='title']", "[class*='Title']"]:
        el = article.select_one(tag)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) > 5:
                address = text
                break

    # If still no address, grab longest text block in the article
    if not address:
        candidates = [el.get_text(strip=True) for el in article.find_all(["p", "span", "div"])
                      if 10 < len(el.get_text(strip=True)) < 120]
        address = max(candidates, key=len, default=area)

    # Description snippet — longest paragraph-like text
    desc = ""
    for el in article.find_all(["p", "span"]):
        text = el.get_text(strip=True)
        if len(text) > len(desc) and "£" not in text:
            desc = text
    desc = desc[:500]

    # Posted date
    posted = None
    for el in article.find_all(True):
        text = el.get_text(strip=True).lower()
        if "added" in text or "reduced" in text or "listed" in text:
            posted = el.get_text(strip=True)
            break

    return {
        "title": address or area,
        "price": price,
        "location": address or area,
        "description": desc,
        "phone": None,
        "posted_date": posted,
        "url": url,
    }


def scrape_area(area: str, min_price: int, max_price: int,
                delay: float = 3.0, keywords: str = "") -> list[dict]:
    new_listings = []
    conn = get_conn()

    for page in range(1, 4):
        url = _build_search_url(area, min_price, max_price, page, keywords)
        logger.info("[OTM] Fetching %s", url)
        soup = fetch(url, delay=delay)
        if not soup:
            break

        articles = soup.select("article")
        logger.info("[OTM] %s page %d: %d article elements found", area, page, len(articles))

        if not articles:
            # Log snippet to diagnose
            logger.warning("[OTM] No articles found. Page title: %s | First 300 chars: %s",
                           soup.title.string if soup.title else "?",
                           soup.get_text()[:300])
            break

        page_count = 0
        for article in articles:
            listing = _parse_article(article, area)
            if not listing or not listing.get("url"):
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
                page_count += 1
                logger.info("[OTM] NEW: %s — £%s", listing.get("title","?")[:60], listing.get("price"))
            except Exception as exc:
                logger.debug("[OTM] DB insert error: %s", exc)

        logger.info("[OTM] %s page %d: %d new listings saved", area, page, page_count)
        if len(articles) < 10:
            break  # last page

    conn.close()
    return new_listings


def run_scraper(config: dict) -> list[dict]:
    all_new = []
    areas = config["scraper"]["target_areas"]
    min_p = config["scraper"]["price"]["min"]
    max_p = config["scraper"]["price"]["max"]
    delay = config["scraper"].get("request_delay", 3)
    kws = config.get("alerts", {}).get("opportunity_keywords", [])

    # Keyword searches that target distressed/undervalued properties.
    # We group keywords into a few focused searches to avoid too many requests.
    keyword_searches = [
        "refurbishment renovation modernisation",
        "no chain chain free probate executor",
        "cash buyer reduced motivated quick sale",
        "project tlc needs updating as seen",
    ]

    for area in areas:
        # General search (all properties in price range)
        try:
            all_new.extend(scrape_area(area, min_p, max_p, delay))
        except Exception as exc:
            logger.error("[OTM] Error scraping %s: %s", area, exc)

        # Keyword-targeted searches
        for kw_group in keyword_searches:
            try:
                all_new.extend(scrape_area(area, min_p, max_p, delay, keywords=kw_group))
            except Exception as exc:
                logger.error("[OTM] Error scraping %s with keywords '%s': %s", area, kw_group, exc)

    logger.info("[OTM] Done. %d new listings.", len(all_new))
    return all_new
