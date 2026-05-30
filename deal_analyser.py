"""
BRR Deal Analyser
=================
Implements the Buy-Refurbish-Refinance framework:

  Offer = GDV - (Profit + GDC)

Where:
  GDV    = Gross Development Value  (from Land Registry sold comps)
  Profit = 25% of GDV              (equity target for refinancing)
  GDC    = Gross Development Cost   (refurb + legals + holding + contingency + SDLT)

A deal PASSES if: asking_price <= max_offer  AND  equity_pct >= 25%

Entry point: run(listings, errors, config)
  - Accepts the list[dict] returned by the scrapers
  - Prints deal sheets to console
  - Exports deals_output_DATE.csv and passing_deals_DATE.csv
  - Sends one email digest with all results
"""

import logging
import os
import re
import smtplib
import statistics
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
SPARQL_ENDPOINT = "https://landregistry.data.gov.uk/app/sparql/query"
EPC_ENDPOINT = "https://epc.opendatacommunities.org/api/v1/domestic/search"

# ─────────────────────────────────────────────────────────────────────────────
# FIELD EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.IGNORECASE
)


def extract_postcode(text: str) -> str | None:
    m = _POSTCODE_RE.search(text or "")
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    return None


def postcode_district(postcode: str) -> str:
    """'LS6 1AB' → 'LS6'"""
    return postcode.split()[0].upper() if postcode else ""


def extract_beds(text: str) -> int:
    """Return number of bedrooms, default 2."""
    m = re.search(r"(\d)\s*(?:bed(?:room)?s?|br\b)", text or "", re.IGNORECASE)
    return int(m.group(1)) if m else 2


def extract_property_type(text: str) -> str:
    """Return Land Registry type code: D / S / T / F."""
    t = (text or "").lower()
    if any(w in t for w in ["detached"]) and "semi" not in t:
        return "D"
    if "semi" in t:
        return "S"
    if any(w in t for w in ["flat", "apartment", "maisonette", "studio"]):
        return "F"
    if any(w in t for w in ["terraced", "terrace", "town house", "townhouse",
                             "end of terrace", "mid terrace"]):
        return "T"
    if "bungalow" in t:
        return "D"
    return "T"  # terraced is safest default for investment stock


_TYPE_LABEL = {"D": "Detached", "S": "Semi-Detached", "T": "Terraced", "F": "Flat"}

# ─────────────────────────────────────────────────────────────────────────────
# REFURB CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

_REFURB_BANDS = [
    (
        "Heavy",
        40_000,
        ["structural", "full refurb", "full renovation", "uninhabitable",
         "development opportunity", "planning", "derelict", "dilapidated",
         "complete refurbishment", "gutted", "shell"],
    ),
    (
        "Light",
        5_000,
        ["cosmetic", "decorative", "recently updated", "recently renovated",
         "modern throughout", "newly refurbished", "move in ready",
         "move-in ready", "immaculate", "pristine", "excellent condition"],
    ),
    (
        "Medium",
        20_000,
        ["dated", "needs modernisation", "needs modernizing", "needs updating",
         "requires updating", "original features", "old kitchen", "old bathroom",
         "needs work", "tlc", "tender loving care", "in need of",
         "scope for improvement", "potential", "project", "tired",
         "as seen", "sold as seen", "no chain", "chain free",
         "probate", "executor", "cash buyer"],
    ),
]


def classify_refurb(description: str) -> tuple[str, int, list[str]]:
    """
    Returns (level_label, refurb_cost, matched_keywords).
    Checks Heavy first (highest precedence), then Light, then Medium.
    Falls back to Medium if nothing matches.
    """
    text = (description or "").lower()
    for level, cost, keywords in _REFURB_BANDS:
        matched = [kw for kw in keywords if kw in text]
        if matched:
            return level, cost, matched
    return "Medium", 20_000, []  # default


# ─────────────────────────────────────────────────────────────────────────────
# SDLT CALCULATION (investor / additional dwelling rates)
# ─────────────────────────────────────────────────────────────────────────────

def calc_sdlt(price: int) -> int:
    """
    Simplified investor SDLT (additional dwelling surcharge).
    Uses flat-rate bands as specified:
      ≤ £250,000        → 3%
      £250,001–£925,000 → 5%
    """
    if price <= 250_000:
        return int(price * 0.03)
    return int(price * 0.05)


# ─────────────────────────────────────────────────────────────────────────────
# GDC CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

LEGALS = 1_500
HOLDING = 2_000


def calc_gdc(refurb_cost: int, price: int) -> dict:
    contingency = int(refurb_cost * 0.15)
    stamp_duty = calc_sdlt(price)
    total = refurb_cost + contingency + LEGALS + HOLDING + stamp_duty
    return {
        "refurb": refurb_cost,
        "contingency": contingency,
        "legals": LEGALS,
        "holding": HOLDING,
        "stamp_duty": stamp_duty,
        "total": total,
    }


# ─────────────────────────────────────────────────────────────────────────────
# LAND REGISTRY — GDV from sold comps
# ─────────────────────────────────────────────────────────────────────────────

_TYPE_URI = {
    "D": "detached",
    "S": "semi-detached",
    "T": "terraced",
    "F": "flat-maisonette",
}


def _sparql_comps(district: str, prop_type: str, months_back: int = 24) -> list[int]:
    cutoff = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")
    type_uri = _TYPE_URI.get(prop_type, "terraced")

    query = f"""
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

SELECT ?amount ?date WHERE {{
  ?tranx lrppi:pricePaid ?amount ;
         lrppi:transactionDate ?date ;
         lrppi:propertyType lrppi:{type_uri} ;
         lrppi:propertyAddress ?addr .
  ?addr lrcommon:postcode ?postcode .
  FILTER(STRSTARTS(STR(?postcode), "{district}"))
  FILTER(?date >= "{cutoff}"^^xsd:date)
}}
ORDER BY DESC(?date)
LIMIT 60
"""
    try:
        resp = requests.get(
            SPARQL_ENDPOINT,
            params={"query": query, "output": "json"},
            headers={"Accept": "application/sparql-results+json"},
            timeout=20,
        )
        resp.raise_for_status()
        bindings = resp.json()["results"]["bindings"]
        prices = [int(float(b["amount"]["value"])) for b in bindings if b.get("amount")]
        logger.info("[LR] %s %s: %d comps returned", district, prop_type, len(prices))
        return prices
    except Exception as exc:
        logger.warning("[LR] SPARQL failed for %s: %s", district, exc)
        return []


def _rest_comps(postcode: str, months_back: int = 24) -> list[int]:
    """Fallback: REST endpoint at exact postcode level (no type filter)."""
    cutoff = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")
    url = (
        "https://landregistry.data.gov.uk/data/ppi/transaction-record.json"
        f"?_page=0&_pageSize=50&propertyAddress.postcode={requests.utils.quote(postcode)}"
        "&_sort=-transactionDate"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("result", {}).get("items", [])
        prices = []
        for item in items:
            if item.get("transactionDate", "")[:10] >= cutoff:
                p = item.get("pricePaid")
                if p:
                    prices.append(int(p))
        logger.info("[LR] REST %s: %d comps", postcode, len(prices))
        return prices
    except Exception as exc:
        logger.warning("[LR] REST failed for %s: %s", postcode, exc)
        return []


def get_gdv_from_comps(postcode: str, prop_type: str) -> dict:
    """
    Returns {gdv, method, comps_count} or {gdv: None} if no data.
    Tries SPARQL on district first, then REST on exact postcode.
    """
    district = postcode_district(postcode)
    prices = _sparql_comps(district, prop_type)

    if not prices and postcode:
        prices = _rest_comps(postcode)

    if not prices:
        return {"gdv": None, "method": "unavailable", "comps_count": 0}

    gdv = int(statistics.median(prices))
    return {
        "gdv": gdv,
        "method": f"median of {len(prices)} comps ({district} district)",
        "comps_count": len(prices),
        "comp_prices": prices,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EPC API — floor area (optional, improves GDV accuracy)
# ─────────────────────────────────────────────────────────────────────────────

def get_epc_floor_area(postcode: str) -> float | None:
    """
    Query EPC register for floor area of properties at this postcode.
    Returns median floor area in m² or None if unavailable.
    Requires EPC_API_KEY in environment (free registration at
    https://epc.opendatacommunities.org/).
    """
    api_key = os.environ.get("EPC_API_KEY", "")
    if not api_key:
        return None

    try:
        time.sleep(2)
        resp = requests.get(
            EPC_ENDPOINT,
            params={"postcode": postcode, "size": 20},
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {api_key}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        areas = []
        for row in rows:
            area = row.get("total-floor-area") or row.get("floor-area-band")
            if area:
                try:
                    areas.append(float(area))
                except (ValueError, TypeError):
                    pass
        if areas:
            return statistics.median(areas)
    except Exception as exc:
        logger.debug("[EPC] Request failed for %s: %s", postcode, exc)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FINDERS FEE LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def calc_finders_fee(equity_pct: float, headroom: int) -> tuple[int, str]:
    if equity_pct >= 30:
        fee = 4_500
        justification = (
            f"Equity of {equity_pct:.1f}% created — exceptional BRR deal, "
            "well above 25% target. Fee reflects premium deal quality."
        )
    elif equity_pct >= 25:
        fee = 3_500
        justification = (
            f"Equity of {equity_pct:.1f}% created — strong BRR deal, "
            "hits 25% target. Fee reflects above-target margin."
        )
    elif equity_pct >= 20:
        fee = 2_500
        justification = (
            f"Equity of {equity_pct:.1f}% at asking — decent deal but below 25% target. "
            f"Passes if negotiated down by £{abs(headroom):,}. Fee reflects margin risk."
        )
    else:
        fee = 1_500
        justification = (
            f"Equity of {equity_pct:.1f}% is marginal. Only viable for highly "
            "motivated buyers or with significant negotiation. Fee reflects effort and risk."
        )
    return fee, justification


# ─────────────────────────────────────────────────────────────────────────────
# VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def calc_verdict(asking: int, max_offer: int, equity_pct: float) -> str:
    if equity_pct >= 25 and asking <= max_offer:
        return "PASSES"
    gap_pct = (asking - max_offer) / asking * 100 if asking > 0 else 999
    if gap_pct <= 10:
        return "NEGOTIATE"
    return "AVOID"


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE DEAL ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyse_one(listing: dict) -> dict:
    """Run full BRR analysis on one listing dict. Returns enriched dict."""
    address = listing.get("title") or listing.get("location") or "Unknown"
    url = listing.get("url", "")
    source = listing.get("source", "")
    asking = listing.get("price") or 0
    description = listing.get("description") or ""
    full_text = f"{address} {description}"

    postcode = extract_postcode(full_text) or extract_postcode(listing.get("location", ""))
    beds = extract_beds(full_text)
    prop_type = extract_property_type(full_text)
    refurb_level, refurb_cost, refurb_kws = classify_refurb(description)

    gdc = calc_gdc(refurb_cost, asking)
    gdv_data = {}
    floor_area = None

    if postcode:
        print(f"  → Querying Land Registry for {postcode_district(postcode)} {_TYPE_LABEL[prop_type]}...")
        time.sleep(2)
        gdv_data = get_gdv_from_comps(postcode, prop_type)

        # Optional EPC floor area
        floor_area = get_epc_floor_area(postcode)
        if floor_area:
            print(f"  → EPC floor area: {floor_area:.0f}m²")
    else:
        logger.warning("[Analyser] No postcode found in: %s", address[:60])
        gdv_data = {"gdv": None, "method": "no postcode", "comps_count": 0}

    gdv = gdv_data.get("gdv")

    if gdv:
        profit_target = int(gdv * 0.25)
        max_offer = gdv - profit_target - gdc["total"]
        headroom = max_offer - asking
        equity_created = gdv - (asking + gdc["total"])
        equity_pct = round(equity_created / gdv * 100, 1) if gdv else 0.0
        verdict = calc_verdict(asking, max_offer, equity_pct)
        finders_fee, fee_justification = calc_finders_fee(equity_pct, headroom)
    else:
        profit_target = max_offer = headroom = equity_created = 0
        equity_pct = 0.0
        verdict = "NO DATA"
        finders_fee = 0
        fee_justification = "GDV could not be estimated — no Land Registry comps found."

    return {
        # Listing fields
        "address": address,
        "url": url,
        "source": source,
        "asking_price": asking,
        "beds": beds,
        "property_type": _TYPE_LABEL.get(prop_type, prop_type),
        "postcode": postcode or "Unknown",
        "postcode_district": postcode_district(postcode) if postcode else "?",
        "description_snippet": description[:200],
        # Refurb
        "refurb_level": refurb_level,
        "refurb_cost": refurb_cost,
        "refurb_keywords": ", ".join(refurb_kws) if refurb_kws else "default",
        # GDC breakdown
        "gdc_refurb": gdc["refurb"],
        "gdc_contingency": gdc["contingency"],
        "gdc_legals": gdc["legals"],
        "gdc_holding": gdc["holding"],
        "gdc_stamp_duty": gdc["stamp_duty"],
        "gdc_total": gdc["total"],
        # GDV
        "gdv": gdv,
        "gdv_method": gdv_data.get("method", "unavailable"),
        "comps_count": gdv_data.get("comps_count", 0),
        "floor_area_sqm": floor_area,
        # BRR core
        "profit_target": profit_target,
        "max_offer": max_offer,
        "headroom": headroom,
        "equity_created": equity_created,
        "equity_pct": equity_pct,
        # Verdict
        "verdict": verdict,
        "finders_fee": finders_fee,
        "fee_justification": fee_justification,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLE OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def print_deal_sheet(d: dict) -> None:
    gdv_str = f"£{d['gdv']:,}" if d['gdv'] else "N/A"
    offer_str = f"£{d['max_offer']:,}" if d['gdv'] else "N/A"
    headroom = d["headroom"]
    headroom_str = (
        f"+£{headroom:,}  (deal stacks at asking)"
        if headroom >= 0 else
        f"−£{abs(headroom):,}  (negotiate down or walk away)"
    ) if d["gdv"] else "N/A"

    verdict_icons = {"PASSES": "✅ PASSES", "NEGOTIATE": "⚠  NEEDS NEGOTIATION",
                     "AVOID": "❌ AVOID", "NO DATA": "❓ INSUFFICIENT DATA"}

    print(f"""
{"="*60}
DEAL ANALYSIS — {d['address']}
{"="*60}
Listing URL   : {d['url'] or 'N/A'}
Asking Price  : £{d['asking_price']:,}
Property Type : {d['beds']}-bed {d['property_type']}
Refurb Level  : {d['refurb_level']} (keywords: "{d['refurb_keywords']}")

--- NUMBERS ---
GDV           : {gdv_str}  ({d['gdv_method']})
Profit Target : £{d['profit_target']:,}   (25% of GDV)
GDC Breakdown :
  Refurb      : £{d['gdc_refurb']:,}
  Contingency : £{d['gdc_contingency']:,}   (15% of refurb)
  Legals      : £{d['gdc_legals']:,}
  Holding     : £{d['gdc_holding']:,}
  Stamp Duty  : £{d['gdc_stamp_duty']:,}
  TOTAL GDC   : £{d['gdc_total']:,}

Max Offer     : {offer_str}  (GDV − Profit − GDC)
Asking Price  : £{d['asking_price']:,}
Headroom      : {headroom_str}

--- VERDICT ---
Equity Created    : £{d['equity_created']:,}  (if bought at asking)
Equity % of GDV   : {d['equity_pct']:.1f}%{'   ← BELOW 25% TARGET' if d['equity_pct'] < 25 and d['gdv'] else '   ✓ ABOVE 25% TARGET' if d['gdv'] else ''}
Deal Status       : {verdict_icons.get(d['verdict'], d['verdict'])}{'  — offer £'+f"{d['max_offer']:,}" if d['verdict'] == 'NEGOTIATE' else ''}

--- FINDERS FEE ---
Recommended Fee   : £{d['finders_fee']:,}
Justification     : {d['fee_justification']}
{"="*60}""")


def print_ranking_table(deals: list[dict]) -> None:
    if not deals:
        return

    verdict_icons = {"PASSES": "✅ PASSES", "NEGOTIATE": "⚠ NEGOTIATE",
                     "AVOID": "❌ AVOID", "NO DATA": "❓ NO DATA"}

    print(f"\n{'='*90}")
    print("DEAL RANKING — sorted by equity %")
    print(f"{'='*90}")
    header = f"{'RANK':<5} {'ADDRESS':<30} {'ASKING':>9} {'MAX OFFER':>10} {'EQUITY%':>8} {'FEE':>7} STATUS"
    print(header)
    print("-" * 90)

    ranked = sorted(deals, key=lambda d: d.get("equity_pct", -999), reverse=True)
    for i, d in enumerate(ranked, 1):
        addr = d["address"][:28]
        asking = f"£{d['asking_price']//1000}k" if d["asking_price"] else "?"
        offer = f"£{d['max_offer']//1000}k" if d.get("max_offer") else "N/A"
        equity = f"{d['equity_pct']:.1f}%" if d.get("gdv") else "N/A"
        fee = f"£{d['finders_fee']:,}" if d.get("finders_fee") else "—"
        status = verdict_icons.get(d.get("verdict", ""), d.get("verdict", ""))
        print(f"{i:<5} {addr:<30} {asking:>9} {offer:>10} {equity:>8} {fee:>7} {status}")

    print(f"{'='*90}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "address", "url", "source", "asking_price", "beds", "property_type",
    "postcode", "postcode_district",
    "refurb_level", "refurb_cost", "refurb_keywords",
    "gdc_refurb", "gdc_contingency", "gdc_legals", "gdc_holding",
    "gdc_stamp_duty", "gdc_total",
    "gdv", "gdv_method", "comps_count",
    "profit_target", "max_offer", "headroom",
    "equity_created", "equity_pct",
    "verdict", "finders_fee", "fee_justification",
]


def export_csvs(deals: list[dict]) -> tuple[Path, Path]:
    """Export all deals and passing deals to dated CSV files."""
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas not installed — run: pip install pandas")
        raise

    OUTPUT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")

    df = pd.DataFrame(deals, columns=[f for f in CSV_FIELDS if f in (deals[0] if deals else {})])
    all_path = OUTPUT_DIR / f"deals_output_{date_str}.csv"
    df.to_csv(all_path, index=False)
    print(f"\n[Export] All deals → {all_path}")

    passing = [d for d in deals if d.get("verdict") in ("PASSES", "NEGOTIATE")]
    pass_path = OUTPUT_DIR / f"passing_deals_{date_str}.csv"
    if passing:
        pd.DataFrame(passing, columns=[f for f in CSV_FIELDS if f in passing[0]]).to_csv(pass_path, index=False)
    else:
        pass_path.write_text(",".join(CSV_FIELDS) + "\n")
    print(f"[Export] Passing deals ({len(passing)}) → {pass_path}")

    return all_path, pass_path


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def _verdict_colour(verdict: str) -> str:
    return {"PASSES": "#27ae60", "NEGOTIATE": "#f39c12",
            "AVOID": "#e74c3c", "NO DATA": "#95a5a6"}.get(verdict, "#555")


def _verdict_icon(verdict: str) -> str:
    return {"PASSES": "✅ PASSES", "NEGOTIATE": "⚠️ NEGOTIATE",
            "AVOID": "❌ AVOID", "NO DATA": "❓ NO DATA"}.get(verdict, verdict)


def _deal_card_html(d: dict) -> str:
    vc = _verdict_colour(d.get("verdict", ""))
    gdv = f"£{d['gdv']:,}" if d.get("gdv") else "N/A"
    offer = f"£{d['max_offer']:,}" if d.get("gdv") else "N/A"
    headroom = d.get("headroom", 0)
    headroom_html = (
        f"<span style='color:#27ae60'>+£{headroom:,}</span>" if headroom >= 0
        else f"<span style='color:#e74c3c'>−£{abs(headroom):,}</span>"
    ) if d.get("gdv") else "N/A"

    return f"""
<div style="border:1px solid #ddd;border-left:5px solid {vc};border-radius:6px;
            padding:16px;margin-bottom:16px;background:#fff">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;
              flex-wrap:wrap;gap:8px">
    <div>
      <div style="font-size:1em;font-weight:700;color:#1a3a5c">
        {d.get('address','?')[:80]}
      </div>
      <div style="font-size:0.82em;color:#666;margin-top:2px">
        {d.get('beds','?')}-bed {d.get('property_type','?')} &nbsp;|&nbsp;
        {d.get('postcode','?')} &nbsp;|&nbsp;
        Refurb: <strong>{d.get('refurb_level','?')}</strong>
      </div>
    </div>
    <span style="background:{vc};color:#fff;padding:4px 12px;border-radius:20px;
                 font-size:0.8em;font-weight:700;white-space:nowrap">
      {_verdict_icon(d.get('verdict',''))}
    </span>
  </div>

  <table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:0.88em">
    <tr>
      <td style="padding:4px 8px;color:#666">Asking</td>
      <td style="padding:4px 8px;font-weight:700">£{d.get('asking_price',0):,}</td>
      <td style="padding:4px 8px;color:#666">GDV</td>
      <td style="padding:4px 8px;font-weight:700">{gdv}
        <span style="color:#888;font-weight:400;font-size:0.85em">({d.get('comps_count',0)} comps)</span>
      </td>
    </tr>
    <tr style="background:#f9f9f9">
      <td style="padding:4px 8px;color:#666">Max Offer</td>
      <td style="padding:4px 8px;font-weight:700">{offer}</td>
      <td style="padding:4px 8px;color:#666">Headroom</td>
      <td style="padding:4px 8px">{headroom_html}</td>
    </tr>
    <tr>
      <td style="padding:4px 8px;color:#666">GDC Total</td>
      <td style="padding:4px 8px">£{d.get('gdc_total',0):,}</td>
      <td style="padding:4px 8px;color:#666">Equity %</td>
      <td style="padding:4px 8px;font-weight:700;color:{vc}">{d.get('equity_pct',0):.1f}%</td>
    </tr>
    <tr style="background:#f9f9f9">
      <td style="padding:4px 8px;color:#666">Finders Fee</td>
      <td style="padding:4px 8px;font-weight:700;color:#1a3a5c">£{d.get('finders_fee',0):,}</td>
      <td style="padding:4px 8px;color:#666">Stamp Duty</td>
      <td style="padding:4px 8px">£{d.get('gdc_stamp_duty',0):,}</td>
    </tr>
  </table>

  <div style="margin-top:8px;font-size:0.82em;color:#555;font-style:italic">
    {d.get('fee_justification','')}
  </div>

  <div style="margin-top:10px">
    <a href="{d.get('url','#')}"
       style="background:#1a3a5c;color:#fff;padding:6px 16px;border-radius:4px;
              text-decoration:none;font-size:0.82em;font-weight:600">
      View Listing →
    </a>
  </div>
</div>"""


def _ranking_table_html(deals: list[dict]) -> str:
    ranked = sorted(deals, key=lambda d: d.get("equity_pct", -999), reverse=True)
    rows = ""
    for i, d in enumerate(ranked, 1):
        vc = _verdict_colour(d.get("verdict", ""))
        vi = _verdict_icon(d.get("verdict", ""))
        addr = d.get("address", "?")[:40]
        asking = f"£{d.get('asking_price',0)//1000}k"
        offer = f"£{d.get('max_offer',0)//1000}k" if d.get("gdv") else "N/A"
        equity = f"{d.get('equity_pct',0):.1f}%" if d.get("gdv") else "N/A"
        fee = f"£{d.get('finders_fee',0):,}"
        bg = "#f4f8ff" if i % 2 == 0 else "#fff"
        rows += f"""
        <tr style="background:{bg}">
          <td style="padding:7px 10px;font-weight:700;color:#666">{i}</td>
          <td style="padding:7px 10px">
            <a href="{d.get('url','#')}" style="color:#1a3a5c;text-decoration:none">{addr}</a>
          </td>
          <td style="padding:7px 10px;font-weight:700">{asking}</td>
          <td style="padding:7px 10px">{offer}</td>
          <td style="padding:7px 10px;font-weight:700;color:{vc}">{equity}</td>
          <td style="padding:7px 10px">{fee}</td>
          <td style="padding:7px 10px">
            <span style="background:{vc};color:#fff;padding:2px 8px;border-radius:10px;
                         font-size:0.78em;font-weight:700">{vi}</span>
          </td>
        </tr>"""

    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:0.85em">
      <thead>
        <tr style="background:#1a3a5c;color:#fff">
          <th style="padding:8px 10px;text-align:left">#</th>
          <th style="padding:8px 10px;text-align:left">Address</th>
          <th style="padding:8px 10px;text-align:left">Asking</th>
          <th style="padding:8px 10px;text-align:left">Max Offer</th>
          <th style="padding:8px 10px;text-align:left">Equity %</th>
          <th style="padding:8px 10px;text-align:left">Fee</th>
          <th style="padding:8px 10px;text-align:left">Status</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def build_email(deals: list[dict], errors: list[str], timestamp: str) -> tuple[str, str]:
    """Returns (html_body, text_body)."""
    total = len(deals)
    passing = sum(1 for d in deals if d.get("verdict") == "PASSES")
    negotiate = sum(1 for d in deals if d.get("verdict") == "NEGOTIATE")
    avoid = sum(1 for d in deals if d.get("verdict") == "AVOID")

    status_colour = "#27ae60" if passing > 0 else "#f39c12" if negotiate > 0 else "#7f8c8d"

    deal_cards = "".join(_deal_card_html(d) for d in
                         sorted(deals, key=lambda d: d.get("equity_pct", -999), reverse=True))

    errors_html = ""
    if errors:
        errors_html = (
            '<div style="background:#fdecea;border-left:4px solid #e74c3c;'
            'padding:10px 14px;margin-top:16px;border-radius:4px;font-size:0.85em">'
            '<strong>Scraper errors:</strong><ul style="margin:4px 0">'
            + "".join(f"<li>{e}</li>" for e in errors)
            + "</ul></div>"
        )

    no_deals_html = "" if deals else (
        '<div style="text-align:center;padding:40px;color:#7f8c8d">'
        '<div style="font-size:2em">🔍</div>'
        '<p>No new listings found today.</p>'
        '<p style="font-size:0.85em">The scraper ran successfully — check back tomorrow.</p>'
        '</div>'
    )

    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:760px;margin:auto;color:#2c3e50;
                   background:#f4f6f9;padding:20px">
  <div style="background:#1a3a5c;padding:22px 28px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:1.3em">BRR Deal Analysis — Daily Report</h2>
    <p style="color:#aac4e4;margin:5px 0 0;font-size:0.85em">{timestamp}</p>
  </div>
  <div style="background:#fff;border:1px solid #ddd;border-top:none;
              padding:24px 28px;border-radius:0 0 8px 8px">

    <!-- Stats -->
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px">
      <div style="background:#f4f8ff;border-left:4px solid {status_colour};
                  padding:12px 16px;border-radius:4px;flex:1;min-width:100px">
        <div style="font-size:1.8em;font-weight:700;color:{status_colour}">{total}</div>
        <div style="font-size:0.78em;color:#666">Deals analysed</div>
      </div>
      <div style="background:#d5f5e3;border-left:4px solid #27ae60;
                  padding:12px 16px;border-radius:4px;flex:1;min-width:100px">
        <div style="font-size:1.8em;font-weight:700;color:#27ae60">{passing}</div>
        <div style="font-size:0.78em;color:#666">✅ Pass (≥25% equity)</div>
      </div>
      <div style="background:#fff8e1;border-left:4px solid #f39c12;
                  padding:12px 16px;border-radius:4px;flex:1;min-width:100px">
        <div style="font-size:1.8em;font-weight:700;color:#f39c12">{negotiate}</div>
        <div style="font-size:0.78em;color:#666">⚠️ Negotiate</div>
      </div>
      <div style="background:#fdecea;border-left:4px solid #e74c3c;
                  padding:12px 16px;border-radius:4px;flex:1;min-width:100px">
        <div style="font-size:1.8em;font-weight:700;color:#e74c3c">{avoid}</div>
        <div style="font-size:0.78em;color:#666">❌ Avoid</div>
      </div>
    </div>

    <!-- Ranking table -->
    {'<h3 style="color:#1a3a5c;margin:0 0 12px">Deal Ranking</h3>' + _ranking_table_html(deals) if deals else ''}

    <!-- Individual deal cards -->
    {'<h3 style="color:#1a3a5c;margin:24px 0 12px">Deal Sheets</h3>' + deal_cards if deals else no_deals_html}

    {errors_html}

    <p style="color:#aaa;font-size:0.75em;margin-top:24px;border-top:1px solid #eee;
              padding-top:12px">
      Property Pipeline BRR Analyser — GDV from Land Registry Price Paid Data
      (Crown copyright). All figures are estimates — conduct your own due diligence.
    </p>
  </div>
</body></html>"""

    # Plain text
    text = f"BRR Deal Report — {timestamp}\n{'='*60}\n"
    text += f"Total: {total} | Pass: {passing} | Negotiate: {negotiate} | Avoid: {avoid}\n\n"
    for d in sorted(deals, key=lambda d: d.get("equity_pct", -999), reverse=True):
        text += (
            f"\n{d.get('address','?')}\n"
            f"  Asking: £{d.get('asking_price',0):,} | GDV: £{d.get('gdv',0) or 0:,} | "
            f"Max Offer: £{d.get('max_offer',0):,}\n"
            f"  Equity: {d.get('equity_pct',0):.1f}% | Fee: £{d.get('finders_fee',0):,} | "
            f"Status: {d.get('verdict','?')}\n"
            f"  URL: {d.get('url','N/A')}\n"
        )
    if errors:
        text += "\nERRORS\n" + "\n".join(errors)

    return html, text


def send_email(deals: list[dict], errors: list[str], alert_email: str, config: dict) -> bool:
    sender = os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not password:
        logger.error("[Email] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
        return False

    timestamp = datetime.now().strftime("%d %B %Y, %H:%M UTC")
    passing = sum(1 for d in deals if d.get("verdict") == "PASSES")
    negotiate = sum(1 for d in deals if d.get("verdict") == "NEGOTIATE")

    if passing > 0:
        subject = f"✅ {passing} BRR deal{'s' if passing!=1 else ''} PASS — {timestamp[:11]}"
    elif negotiate > 0:
        subject = f"⚠️ {negotiate} deal{'s' if negotiate!=1 else ''} need negotiation — {timestamp[:11]}"
    elif deals:
        subject = f"🔍 {len(deals)} properties analysed, none pass BRR — {timestamp[:11]}"
    else:
        subject = f"Property Pipeline: no new listings today — {timestamp[:11]}"

    html_body, text_body = build_email(deals, errors, timestamp)

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = alert_email
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [alert_email], msg.as_string())
        logger.info("[Email] BRR digest sent → %s", alert_email)
        return True
    except Exception as exc:
        logger.error("[Email] Send failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run(listings: list[dict], errors: list[str], config: dict) -> list[dict]:
    """
    Analyse all listings, print deal sheets, export CSVs, send email.
    Returns list of analysed deal dicts.
    """
    alert_email = os.environ.get("ALERT_EMAIL", "")
    deals = []

    if not listings:
        print("[Analyser] No new listings to analyse.")
        if alert_email and config.get("alerts", {}).get("email_enabled", True):
            send_email([], errors, alert_email, config)
        return []

    print(f"\n[Analyser] Analysing {len(listings)} listing(s) using BRR framework...\n")

    for i, listing in enumerate(listings, 1):
        addr = listing.get("title") or listing.get("location") or "?"
        print(f"[{i}/{len(listings)}] {addr[:70]}")
        try:
            deal = analyse_one(listing)
            deals.append(deal)
            print_deal_sheet(deal)
        except Exception as exc:
            logger.error("[Analyser] Failed on listing %s: %s", addr[:40], exc)

    if deals:
        print_ranking_table(deals)
        try:
            export_csvs(deals)
        except Exception as exc:
            logger.error("[Analyser] CSV export failed: %s", exc)

    if alert_email and config.get("alerts", {}).get("email_enabled", True):
        ok = send_email(deals, errors, alert_email, config)
        print(f"[Analyser] Email {'sent ✓' if ok else 'FAILED ✗'} → {alert_email}")
    else:
        print("[Analyser] No ALERT_EMAIL set — skipping email.")

    return deals
