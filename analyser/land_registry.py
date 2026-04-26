"""
UK Land Registry Price Paid API — free, official government data.

Fetches comparable sold prices for a given postcode or street.
API docs: https://landregistry.data.gov.uk/app/ppd
SPARQL endpoint: https://landregistry.data.gov.uk/app/sparql
"""

import logging
import re
import statistics
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://landregistry.data.gov.uk/app/sparql/query"

# Fallback REST endpoint (simpler, postcode-level)
REST_ENDPOINT = (
    "https://landregistry.data.gov.uk/data/ppi/transaction-record.json"
    "?_page=0&_pageSize=50&propertyAddress.postcode={postcode}&_sort=-transactionDate"
)


def _clean_postcode(postcode: str) -> str:
    return re.sub(r"\s+", " ", postcode.strip().upper())


def fetch_sold_comparables(postcode: str, property_type: str = "D",
                            months_back: int = 24) -> list[dict]:
    """
    Fetch recent sold prices near a postcode from Land Registry.

    property_type: D=Detached, S=Semi, T=Terraced, F=Flat
    Returns list of {'address', 'price', 'date', 'type'} dicts.
    """
    postcode = _clean_postcode(postcode)
    cutoff = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")

    # Try SPARQL first — richer data
    sparql_results = _sparql_query(postcode, property_type, cutoff)
    if sparql_results:
        return sparql_results

    # Fallback to REST endpoint
    return _rest_query(postcode, cutoff)


def _sparql_query(postcode: str, prop_type: str, cutoff: str) -> list[dict]:
    query = f"""
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

SELECT ?paon ?saon ?street ?town ?county ?amount ?date ?category
WHERE {{
  ?tranx lrppi:pricePaid ?amount ;
         lrppi:transactionDate ?date ;
         lrppi:propertyType lrppi:{_type_uri(prop_type)} ;
         lrppi:propertyAddress ?addr .
  ?addr  lrcommon:postcode "{postcode}" ;
         lrcommon:street ?street .
  OPTIONAL {{ ?addr lrcommon:paon ?paon }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon }}
  OPTIONAL {{ ?addr lrcommon:town ?town }}
  FILTER (?date >= "{cutoff}"^^xsd:date)
}}
ORDER BY DESC(?date)
LIMIT 30
"""
    try:
        resp = requests.get(
            SPARQL_ENDPOINT,
            params={"query": query, "output": "json"},
            timeout=15,
            headers={"Accept": "application/sparql-results+json"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for row in data["results"]["bindings"]:
            paon = row.get("paon", {}).get("value", "")
            street = row.get("street", {}).get("value", "")
            results.append({
                "address": f"{paon} {street}".strip(),
                "price": int(float(row["amount"]["value"])),
                "date": row["date"]["value"][:10],
                "type": prop_type,
            })
        logger.info("[LandRegistry] SPARQL returned %d comparables for %s", len(results), postcode)
        return results
    except Exception as exc:
        logger.warning("[LandRegistry] SPARQL failed: %s", exc)
        return []


def _type_uri(code: str) -> str:
    return {
        "D": "detached",
        "S": "semi-detached",
        "T": "terraced",
        "F": "flat-maisonette",
    }.get(code.upper(), "terraced")


def _rest_query(postcode: str, cutoff: str) -> list[dict]:
    url = REST_ENDPOINT.format(postcode=postcode)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("result", {}).get("items", [])
        results = []
        for item in items:
            date_str = item.get("transactionDate", "")[:10]
            if date_str < cutoff:
                continue
            addr = item.get("propertyAddress", {})
            results.append({
                "address": (
                    f"{addr.get('paon','')} {addr.get('street','')} "
                    f"{addr.get('town','')}".strip()
                ),
                "price": item.get("pricePaid", 0),
                "date": date_str,
                "type": item.get("propertyType", "?"),
            })
        logger.info("[LandRegistry] REST returned %d comparables for %s", len(results), postcode)
        return results
    except Exception as exc:
        logger.warning("[LandRegistry] REST failed: %s", exc)
        return []


def estimate_market_value(comparables: list[dict]) -> dict:
    """
    Derive a market value estimate from comparables.
    Returns {'median', 'mean', 'count', 'min', 'max'} or empty dict.
    """
    if not comparables:
        return {}

    prices = [c["price"] for c in comparables if c.get("price")]
    if not prices:
        return {}

    return {
        "median": int(statistics.median(prices)),
        "mean": int(statistics.mean(prices)),
        "count": len(prices),
        "min": min(prices),
        "max": max(prices),
    }
