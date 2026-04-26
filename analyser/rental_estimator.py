"""
Rental income estimator — scrapes Rightmove rental listings for a postcode
to estimate achievable rent for a comparable property.
"""

import json
import logging
import re
import statistics
from urllib.parse import quote_plus

from scraper.base import fetch, SESSION
import time, random

logger = logging.getLogger(__name__)

BASE_URL = "https://www.rightmove.co.uk"
AUTOCOMPLETE_URL = (
    "https://www.rightmove.co.uk/typeAhead/uknoauth?"
    "input={query}&rent=true&sale=false"
)


def _resolve_rental_location(postcode_or_area: str) -> str | None:
    # Use only the outcode (first part) for broader coverage
    outcode = postcode_or_area.split()[0] if " " in postcode_or_area else postcode_or_area
    time.sleep(2 + random.uniform(0.5, 1.0))
    try:
        resp = SESSION.get(
            AUTOCOMPLETE_URL.format(query=quote_plus(outcode)),
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("typeAheadLocations", [])
        if results:
            return results[0].get("locationIdentifier")
    except Exception as exc:
        logger.warning("[Rental] Location lookup failed: %s", exc)
    return None


def _parse_rental_prices(soup) -> list[int]:
    """Extract monthly rental prices from a Rightmove rental search page."""
    prices = []

    # Try JSON model first
    for tag in soup.find_all("script"):
        text = tag.string or ""
        if "jsonModel" in text:
            match = re.search(r"window\.jsonModel\s*=\s*(\{.*?\});", text, re.DOTALL)
            if match:
                try:
                    model = json.loads(match.group(1))
                    for prop in model.get("properties", []):
                        amt = prop.get("price", {}).get("amount")
                        freq = prop.get("price", {}).get("frequency", "monthly")
                        if amt:
                            # Convert weekly to monthly
                            if "week" in str(freq).lower():
                                amt = int(amt * 52 / 12)
                            prices.append(int(amt))
                    return prices
                except Exception:
                    pass

    # Fallback: parse HTML price elements
    for el in soup.select(".propertyCard-priceValue, [data-test='property-price']"):
        raw = el.get_text(strip=True)
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            val = int(digits)
            # Rightmove shows PCM or pw — anything under £400 is likely pw
            if val < 400:
                val = int(val * 52 / 12)
            if 300 <= val <= 10000:
                prices.append(val)

    return prices


def estimate_rental(postcode: str, bedrooms: int = 2, delay: float = 3.0) -> dict:
    """
    Estimate monthly rental income for a property near the given postcode.

    Returns {'median_rent', 'mean_rent', 'sample_size'} or empty dict on failure.
    """
    location_id = _resolve_rental_location(postcode)
    if not location_id:
        logger.warning("[Rental] Could not resolve location for %s", postcode)
        return {}

    url = (
        f"{BASE_URL}/property-to-rent/find.html"
        f"?locationIdentifier={quote_plus(location_id)}"
        f"&minBedrooms={max(1, bedrooms - 1)}"
        f"&maxBedrooms={bedrooms + 1}"
        f"&index=0"
    )

    soup = fetch(url, delay=delay)
    if not soup:
        return {}

    prices = _parse_rental_prices(soup)

    if len(prices) < 3:
        logger.warning("[Rental] Only %d rental comparables found for %s", len(prices), postcode)

    if not prices:
        return {}

    return {
        "median_rent": int(statistics.median(prices)),
        "mean_rent": int(statistics.mean(prices)),
        "sample_size": len(prices),
    }
