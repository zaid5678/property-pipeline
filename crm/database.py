"""
CRM database layer — CRUD operations for investors, deals, and interactions.
"""

import json
import logging
from datetime import datetime, timezone

from db.models import get_conn

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Investors
# ---------------------------------------------------------------------------

def add_investor(name: str, email: str, phone: str = "",
                 areas: list[str] | None = None, strategy: str = "BTL",
                 max_budget: int | None = None, notes: str = "") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO investors (name, email, phone, areas, strategy,
               max_budget, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, email, phone,
             json.dumps(areas or []), strategy, max_budget, notes, _now())
        )
        conn.commit()
        inv_id = cur.lastrowid
        logger.info("[CRM] Added investor: %s (%s)", name, email)
        return inv_id
    finally:
        conn.close()


def update_investor(investor_id: int, **kwargs) -> None:
    allowed = {"name", "email", "phone", "areas", "strategy",
               "max_budget", "notes", "active"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "areas" in fields and isinstance(fields["areas"], list):
        fields["areas"] = json.dumps(fields["areas"])
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE investors SET {set_clause} WHERE id = ?",
            (*fields.values(), investor_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_investor(investor_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM investors WHERE id = ?", (investor_id,)).fetchone()
    conn.close()
    return _investor_row(row) if row else None


def list_investors(active_only: bool = True) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM investors"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name"
    rows = conn.execute(query).fetchall()
    conn.close()
    return [_investor_row(r) for r in rows]


def _investor_row(row) -> dict:
    d = dict(row)
    try:
        d["areas"] = json.loads(d.get("areas") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["areas"] = []
    return d


def find_matching_investors(deal: dict) -> list[dict]:
    """
    Return investors whose criteria match the deal:
      - deal area is in investor's preferred areas (case-insensitive, partial)
      - deal purchase price <= investor's max_budget (if set)
    """
    all_investors = list_investors(active_only=True)
    address_lower = (deal.get("address", "") + " " + deal.get("postcode", "")).lower()
    matches = []
    for inv in all_investors:
        # Budget check
        budget = inv.get("max_budget")
        price = deal.get("purchase_price", 0)
        if budget and price and price > budget:
            continue
        # Area check — if investor has no areas specified, send to all
        areas = inv.get("areas", [])
        if not areas:
            matches.append(inv)
            continue
        if any(a.lower() in address_lower for a in areas):
            matches.append(inv)
    return matches


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------

def save_deal(deal: dict) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO deals (address, purchase_price, market_value, bmv_percent,
               gross_yield, net_yield, monthly_rent, pass_fail, notes, status,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'analysed', ?, ?)""",
            (
                deal.get("address"), deal.get("purchase_price"), deal.get("market_value"),
                deal.get("bmv_percent"), deal.get("gross_yield"), deal.get("net_yield"),
                deal.get("monthly_rent"), deal.get("pass_fail"), deal.get("notes"),
                _now(), _now(),
            )
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_deal_status(deal_id: int, status: str, notes: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE deals SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
            (status, notes, _now(), deal_id)
        )
        conn.commit()
    finally:
        conn.close()


def list_deals(status: str | None = None) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM deals"
    params = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_deal(deal_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Investor–Deal interactions
# ---------------------------------------------------------------------------

def record_deal_sent(deal_id: int, investor_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO deal_investors (deal_id, investor_id, sent_at)
               VALUES (?, ?, ?)""",
            (deal_id, investor_id, _now())
        )
        conn.execute(
            """INSERT INTO interactions (investor_id, deal_id, type, notes, created_at)
               VALUES (?, ?, 'email_sent', 'Deal email sent', ?)""",
            (investor_id, deal_id, _now())
        )
        conn.commit()
    finally:
        conn.close()


def record_response(deal_id: int, investor_id: int, response: str) -> None:
    """response: 'interested' | 'not_interested'"""
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE deal_investors SET response = ?, responded_at = ?
               WHERE deal_id = ? AND investor_id = ?""",
            (response, _now(), deal_id, investor_id)
        )
        conn.execute(
            """INSERT INTO interactions (investor_id, deal_id, type, notes, created_at)
               VALUES (?, ?, 'note', ?, ?)""",
            (investor_id, deal_id, f"Response recorded: {response}", _now())
        )
        conn.commit()
    finally:
        conn.close()


def get_follow_up_needed(hours: int = 48) -> list[dict]:
    """Return deal_investor rows with no response after `hours` hours."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = get_conn()
    rows = conn.execute(
        """SELECT di.*, i.name, i.email, d.address
           FROM deal_investors di
           JOIN investors i ON i.id = di.investor_id
           JOIN deals d ON d.id = di.deal_id
           WHERE di.response IS NULL AND di.sent_at < ?""",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

def record_fee(deal_id: int, investor_id: int, amount: int, notes: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO fees (deal_id, investor_id, amount, received_at, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (deal_id, investor_id, amount, _now(), notes)
        )
        conn.commit()
    finally:
        conn.close()


def total_fees_earned() -> int:
    conn = get_conn()
    result = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM fees").fetchone()
    conn.close()
    return result[0]
