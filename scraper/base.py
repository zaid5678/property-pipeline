"""Shared utilities for all scrapers."""

import hashlib
import logging
import time
import random
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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


def fetch(url: str, delay: float = 3.0) -> BeautifulSoup | None:
    """GET a URL and return a BeautifulSoup object, or None on failure."""
    # Polite jitter so we don't hammer servers
    time.sleep(delay + random.uniform(0.5, 1.5))
    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        logger.warning("Fetch failed for %s: %s", url, exc)
        return None


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
