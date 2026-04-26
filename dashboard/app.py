"""
Flask dashboard — property pipeline web UI.

Run with:  python dashboard/app.py
Then open: http://localhost:5000
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from dotenv import load_dotenv

load_dotenv()

from db.models import init_db, get_conn
from crm import database as crm_db
import yaml

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "property-pipeline-secret-2024")


def _load_config() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Routes — Properties
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    conn = get_conn()
    stats = {
        "total_properties": conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
        "new_today": conn.execute(
            "SELECT COUNT(*) FROM properties WHERE DATE(scraped_at) = DATE('now')"
        ).fetchone()[0],
        "total_deals": conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0],
        "total_investors": conn.execute("SELECT COUNT(*) FROM investors WHERE active=1").fetchone()[0],
        "fees_earned": conn.execute("SELECT COALESCE(SUM(amount),0) FROM fees").fetchone()[0],
        "pipeline": {},
    }

    for status in ["found", "analysed", "sent", "under_offer", "completed", "dead"]:
        count = conn.execute(
            "SELECT COUNT(*) FROM deals WHERE status=?", (status,)
        ).fetchone()[0]
        stats["pipeline"][status] = count

    follow_ups = crm_db.get_follow_up_needed(
        _load_config().get("crm", {}).get("follow_up_hours", 48)
    )
    stats["follow_ups_needed"] = len(follow_ups)
    conn.close()
    return render_template("index.html", stats=stats)


@app.route("/properties")
def properties():
    conn = get_conn()
    source = request.args.get("source", "")
    search = request.args.get("search", "")
    min_price = request.args.get("min_price", "")
    max_price = request.args.get("max_price", "")

    query = "SELECT * FROM properties WHERE 1=1"
    params = []
    if source:
        query += " AND source = ?"
        params.append(source)
    if search:
        query += " AND (title LIKE ? OR location LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if min_price:
        query += " AND price >= ?"
        params.append(int(min_price))
    if max_price:
        query += " AND price <= ?"
        params.append(int(max_price))

    query += " ORDER BY scraped_at DESC LIMIT 200"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return render_template("properties.html", properties=rows,
                           source=source, search=search,
                           min_price=min_price, max_price=max_price)


# ---------------------------------------------------------------------------
# Routes — Deal Pipeline
# ---------------------------------------------------------------------------

@app.route("/pipeline")
def pipeline():
    deals_by_status = {}
    for status in ["found", "analysed", "sent", "under_offer", "completed", "dead"]:
        deals_by_status[status] = crm_db.list_deals(status)
    return render_template("pipeline.html", deals_by_status=deals_by_status)


@app.route("/deals/<int:deal_id>/status", methods=["POST"])
def update_deal_status(deal_id):
    new_status = request.form.get("status")
    notes = request.form.get("notes", "")
    if new_status:
        crm_db.update_deal_status(deal_id, new_status, notes)
        flash(f"Deal {deal_id} moved to '{new_status}'", "success")
    return redirect(url_for("pipeline"))


# ---------------------------------------------------------------------------
# Routes — Investors
# ---------------------------------------------------------------------------

@app.route("/investors")
def investors():
    rows = crm_db.list_investors(active_only=False)
    follow_ups = crm_db.get_follow_up_needed(
        _load_config().get("crm", {}).get("follow_up_hours", 48)
    )
    follow_up_ids = {r["investor_id"] for r in follow_ups}
    return render_template("investors.html", investors=rows, follow_up_ids=follow_up_ids)


@app.route("/investors/add", methods=["GET", "POST"])
def add_investor():
    if request.method == "POST":
        areas_raw = request.form.get("areas", "")
        areas = [a.strip() for a in areas_raw.split(",") if a.strip()]
        budget_raw = request.form.get("max_budget", "0")
        budget = int(budget_raw) if budget_raw.isdigit() and int(budget_raw) > 0 else None
        crm_db.add_investor(
            name=request.form["name"],
            email=request.form["email"],
            phone=request.form.get("phone", ""),
            areas=areas,
            strategy=request.form.get("strategy", "BTL"),
            max_budget=budget,
            notes=request.form.get("notes", ""),
        )
        flash("Investor added successfully.", "success")
        return redirect(url_for("investors"))
    return render_template("add_investor.html")


# ---------------------------------------------------------------------------
# API endpoints for AJAX
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    data = {
        "total_properties": conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
        "total_deals": conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0],
        "fees_earned": conn.execute("SELECT COALESCE(SUM(amount),0) FROM fees").fetchone()[0],
    }
    conn.close()
    return jsonify(data)


@app.route("/api/properties/recent")
def api_recent_properties():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM properties ORDER BY scraped_at DESC LIMIT 10"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
