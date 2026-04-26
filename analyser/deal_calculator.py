"""
Deal calculator — combines Land Registry comparables and rental estimates
to produce a full deal analysis with PASS/FAIL recommendation.
"""

import logging
from datetime import datetime, timezone

from analyser.land_registry import fetch_sold_comparables, estimate_market_value
from analyser.rental_estimator import estimate_rental

logger = logging.getLogger(__name__)


def analyse_deal(
    address: str,
    postcode: str,
    purchase_price: int,
    bedrooms: int = 2,
    property_type: str = "T",
    config: dict | None = None,
) -> dict:
    """
    Full deal analysis.

    Returns a result dict with all figures and a PASS/FAIL recommendation.
    """
    cfg = config or {}
    analysis_cfg = cfg.get("analysis", {})
    min_bmv = analysis_cfg.get("min_bmv_percent", 15)
    min_yield = analysis_cfg.get("min_gross_yield", 7.0)
    costs_pct = analysis_cfg.get("assumed_costs_percent", 25)

    print(f"\n[Analyser] Fetching sold comparables for {postcode}...")
    comparables = fetch_sold_comparables(postcode, property_type)
    market_stats = estimate_market_value(comparables)

    print(f"[Analyser] Fetching rental estimates for {postcode}...")
    rental_data = estimate_rental(postcode, bedrooms)

    # Market value — use median if available
    market_value = market_stats.get("median") if market_stats else None

    # BMV calculation
    bmv_percent = None
    if market_value and market_value > 0:
        bmv_percent = round((market_value - purchase_price) / market_value * 100, 2)

    # Yield calculations
    monthly_rent = rental_data.get("median_rent")
    annual_rent = monthly_rent * 12 if monthly_rent else None

    gross_yield = None
    net_yield = None
    if annual_rent and purchase_price > 0:
        gross_yield = round(annual_rent / purchase_price * 100, 2)
        net_rent = annual_rent * (1 - costs_pct / 100)
        net_yield = round(net_rent / purchase_price * 100, 2)

    # PASS/FAIL
    passes_bmv = (bmv_percent is not None and bmv_percent >= min_bmv)
    passes_yield = (gross_yield is not None and gross_yield >= min_yield)

    if passes_bmv and passes_yield:
        pass_fail = "PASS"
    elif bmv_percent is None or gross_yield is None:
        pass_fail = "INCOMPLETE"
    else:
        reasons = []
        if not passes_bmv:
            reasons.append(f"BMV {bmv_percent:.1f}% < {min_bmv}% threshold")
        if not passes_yield:
            reasons.append(f"Yield {gross_yield:.1f}% < {min_yield}% threshold")
        pass_fail = "FAIL — " + "; ".join(reasons)

    result = {
        "address": address,
        "postcode": postcode,
        "purchase_price": purchase_price,
        "bedrooms": bedrooms,
        "property_type": property_type,
        "market_value": market_value,
        "market_value_median": market_stats.get("median"),
        "market_value_mean": market_stats.get("mean"),
        "comparables_count": market_stats.get("count", 0),
        "monthly_rent": monthly_rent,
        "annual_rent": annual_rent,
        "rental_sample_size": rental_data.get("sample_size", 0),
        "bmv_percent": bmv_percent,
        "gross_yield": gross_yield,
        "net_yield": net_yield,
        "costs_percent": costs_pct,
        "pass_fail": pass_fail,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
        "comparables": comparables[:10],  # store top 10 for the PDF
    }

    _print_summary(result)
    return result


def _print_summary(r: dict) -> None:
    pf = r["pass_fail"]
    pf_icon = "✅" if pf == "PASS" else ("⚠️" if pf == "INCOMPLETE" else "❌")

    print(f"""
{'='*50}
DEAL ANALYSIS — {r['address']}
{'='*50}
Purchase Price:    £{r['purchase_price']:,}
Market Value:      £{r['market_value']:,} (from {r['comparables_count']} comps)
Below Market Value:{r['bmv_percent']:.1f}% BMV
Monthly Rent:      £{r['monthly_rent']:,} (from {r['rental_sample_size']} rentals)
Annual Rent:       £{r['annual_rent']:,}
Gross Yield:       {r['gross_yield']:.1f}%
Net Yield:         {r['net_yield']:.1f}% (after {r['costs_percent']}% costs)
{'='*50}
VERDICT:  {pf_icon}  {r['pass_fail']}
{'='*50}
""")
