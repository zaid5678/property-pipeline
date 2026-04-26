"""
CRM command-line interface.

Usage:
  python -m crm.cli investors list
  python -m crm.cli investors add
  python -m crm.cli deals list
  python -m crm.cli deals send <deal_id>
  python -m crm.cli deals analyse
  python -m crm.cli follow-up
  python -m crm.cli fee record
"""

import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import click
from tabulate import tabulate
from dotenv import load_dotenv

load_dotenv()

from db.models import init_db
from crm import database as db
from analyser.deal_calculator import analyse_deal
from alerts.emailer import send_deal_to_investor
from analyser.pdf_generator import generate_deal_pdf
import yaml


def _load_config() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


@click.group()
def cli():
    """Property Pipeline CRM — manage investors, deals, and fees."""
    init_db()


# ---------------------------------------------------------------------------
# Investor commands
# ---------------------------------------------------------------------------

@cli.group()
def investors():
    """Manage the investor database."""


@investors.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include inactive investors")
def investors_list(show_all):
    """List all investors."""
    rows = db.list_investors(active_only=not show_all)
    if not rows:
        click.echo("No investors found.")
        return
    table = [
        [r["id"], r["name"], r["email"], r.get("phone", "—"),
         r.get("strategy", "—"), f"£{r['max_budget']:,}" if r.get("max_budget") else "—",
         ", ".join(r.get("areas", [])) or "Any",
         "Active" if r.get("active") else "Inactive"]
        for r in rows
    ]
    click.echo(tabulate(
        table,
        headers=["ID", "Name", "Email", "Phone", "Strategy", "Budget", "Areas", "Status"],
        tablefmt="rounded_outline"
    ))


@investors.command("add")
def investors_add():
    """Interactively add a new investor."""
    click.echo("=== Add New Investor ===")
    name = click.prompt("Full name")
    email = click.prompt("Email address")
    phone = click.prompt("Phone number", default="")
    strategy = click.prompt(
        "Strategy (BTL/HMO/SA/FLIP)", default="BTL",
        type=click.Choice(["BTL", "HMO", "SA", "FLIP"], case_sensitive=False)
    )
    areas_raw = click.prompt(
        "Preferred areas (comma-separated, or leave blank for any)", default=""
    )
    areas = [a.strip() for a in areas_raw.split(",") if a.strip()]
    budget_raw = click.prompt("Max budget (£, or 0 for no limit)", default="0")
    budget = int(budget_raw) if budget_raw.strip().isdigit() else None
    notes = click.prompt("Notes", default="")

    inv_id = db.add_investor(
        name=name, email=email, phone=phone,
        areas=areas, strategy=strategy,
        max_budget=budget if budget else None,
        notes=notes
    )
    click.echo(f"\n✅ Investor added with ID {inv_id}: {name} ({email})")


@investors.command("update")
@click.argument("investor_id", type=int)
def investors_update(investor_id):
    """Update an investor record."""
    inv = db.get_investor(investor_id)
    if not inv:
        click.echo(f"❌ Investor {investor_id} not found.")
        return

    click.echo(f"Updating investor: {inv['name']} — leave blank to keep current value")
    name = click.prompt("Name", default=inv["name"])
    phone = click.prompt("Phone", default=inv.get("phone", ""))
    strategy = click.prompt("Strategy", default=inv.get("strategy", "BTL"))
    areas_raw = click.prompt(
        "Areas", default=", ".join(inv.get("areas", []))
    )
    areas = [a.strip() for a in areas_raw.split(",") if a.strip()]
    budget_raw = click.prompt(
        "Max budget (0 = no limit)", default=str(inv.get("max_budget") or 0)
    )
    budget = int(budget_raw) if budget_raw.strip().isdigit() else None
    notes = click.prompt("Notes", default=inv.get("notes", ""))
    active = click.confirm("Active?", default=bool(inv.get("active", 1)))

    db.update_investor(
        investor_id, name=name, phone=phone, strategy=strategy,
        areas=areas, max_budget=budget if budget else None,
        notes=notes, active=1 if active else 0
    )
    click.echo(f"✅ Investor {investor_id} updated.")


# ---------------------------------------------------------------------------
# Deal commands
# ---------------------------------------------------------------------------

@cli.group()
def deals():
    """Manage the deal pipeline."""


@deals.command("list")
@click.option("--status", default=None,
              help="Filter by status: found/analysed/sent/under_offer/completed/dead")
def deals_list(status):
    """List deals in the pipeline."""
    rows = db.list_deals(status)
    if not rows:
        click.echo("No deals found.")
        return
    table = [
        [r["id"], r.get("address", "—")[:40],
         f"£{r['purchase_price']:,}" if r.get("purchase_price") else "—",
         f"{r['bmv_percent']:.1f}%" if r.get("bmv_percent") else "—",
         f"{r['gross_yield']:.1f}%" if r.get("gross_yield") else "—",
         r.get("pass_fail", "—")[:10],
         r.get("status", "—")]
        for r in rows
    ]
    click.echo(tabulate(
        table,
        headers=["ID", "Address", "Price", "BMV", "Yield", "Verdict", "Status"],
        tablefmt="rounded_outline"
    ))


@deals.command("analyse")
def deals_analyse():
    """Run a deal analysis for a new property."""
    cfg = _load_config()
    click.echo("=== Deal Analyser ===")
    address = click.prompt("Full property address")
    postcode = click.prompt("Postcode")
    purchase_price = click.prompt("Asking/purchase price (£)", type=int)
    bedrooms = click.prompt("Number of bedrooms", type=int, default=2)
    prop_type = click.prompt(
        "Property type (D=Detached, S=Semi, T=Terraced, F=Flat)",
        default="T",
        type=click.Choice(["D", "S", "T", "F"], case_sensitive=False)
    )

    result = analyse_deal(
        address=address,
        postcode=postcode,
        purchase_price=purchase_price,
        bedrooms=bedrooms,
        property_type=prop_type.upper(),
        config=cfg,
    )

    if click.confirm("\nSave this deal to the database?", default=True):
        result["notes"] = click.prompt("Notes (optional)", default="")
        deal_id = db.save_deal(result)
        click.echo(f"✅ Deal saved with ID {deal_id}")

        if click.confirm("Generate PDF deal pack?", default=True):
            fee = cfg.get("deals", {}).get("sourcing_fee", 3000)
            pdf_path = generate_deal_pdf(result, sourcing_fee=fee)
            click.echo(f"✅ PDF saved: {pdf_path}")


@deals.command("status")
@click.argument("deal_id", type=int)
@click.argument("new_status", type=click.Choice(
    ["found", "analysed", "sent", "under_offer", "completed", "dead"],
    case_sensitive=False
))
def deals_status(deal_id, new_status):
    """Update the status of a deal."""
    notes = click.prompt("Notes (optional)", default="")
    db.update_deal_status(deal_id, new_status, notes)
    click.echo(f"✅ Deal {deal_id} status updated to '{new_status}'")


@deals.command("send")
@click.argument("deal_id", type=int)
def deals_send(deal_id):
    """Email a deal to all matching investors."""
    cfg = _load_config()
    deal = db.get_deal(deal_id)
    if not deal:
        click.echo(f"❌ Deal {deal_id} not found.")
        return

    investors = db.find_matching_investors(deal)
    if not investors:
        click.echo("❌ No matching investors found. Add investors first.")
        return

    click.echo(f"\nFound {len(investors)} matching investor(s):")
    for inv in investors:
        click.echo(f"  • {inv['name']} ({inv['email']})")

    if not click.confirm("\nSend deal email to all these investors?"):
        return

    fee = cfg.get("deals", {}).get("sourcing_fee", 3000)

    # Generate PDF attachment
    pdf_path = None
    if click.confirm("Attach PDF deal pack?", default=True):
        pdf_path = generate_deal_pdf(deal, sourcing_fee=fee)

    sent = 0
    for inv in investors:
        ok = send_deal_to_investor(
            deal=deal,
            investor=inv,
            sourcing_fee=fee,
            attachments=[pdf_path] if pdf_path else None,
        )
        if ok:
            db.record_deal_sent(deal_id, inv["id"])
            sent += 1
            click.echo(f"  ✅ Sent to {inv['name']}")
        else:
            click.echo(f"  ❌ Failed to send to {inv['name']}")

    db.update_deal_status(deal_id, "sent")
    click.echo(f"\n✅ Deal emailed to {sent}/{len(investors)} investors.")


@deals.command("respond")
@click.argument("deal_id", type=int)
@click.argument("investor_id", type=int)
@click.argument("response", type=click.Choice(
    ["interested", "not_interested"], case_sensitive=False
))
def deals_respond(deal_id, investor_id, response):
    """Log an investor's response to a deal."""
    db.record_response(deal_id, investor_id, response)
    click.echo(f"✅ Response '{response}' recorded for investor {investor_id} on deal {deal_id}")


# ---------------------------------------------------------------------------
# Follow-up
# ---------------------------------------------------------------------------

@cli.command("follow-up")
def follow_up():
    """List investors who haven't responded to deal emails in 48h."""
    cfg = _load_config()
    hours = cfg.get("crm", {}).get("follow_up_hours", 48)
    rows = db.get_follow_up_needed(hours)
    if not rows:
        click.echo(f"✅ No follow-ups needed (threshold: {hours}h)")
        return
    click.echo(f"\n⚠️  {len(rows)} investor(s) need following up:\n")
    table = [
        [r["investor_id"], r.get("name", "—"), r.get("email", "—"),
         r.get("address", "—")[:40], r.get("sent_at", "—")[:16]]
        for r in rows
    ]
    click.echo(tabulate(
        table,
        headers=["Investor ID", "Name", "Email", "Deal", "Email Sent At"],
        tablefmt="rounded_outline"
    ))


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

@cli.command("fee")
def fee_record():
    """Record a sourcing fee received."""
    deal_id = click.prompt("Deal ID", type=int)
    investor_id = click.prompt("Investor ID", type=int)
    amount = click.prompt("Fee amount (£)", type=int)
    notes = click.prompt("Notes", default="")
    db.record_fee(deal_id, investor_id, amount, notes)
    total = db.total_fees_earned()
    click.echo(f"✅ Fee of £{amount:,} recorded. Total earned: £{total:,}")


if __name__ == "__main__":
    cli()
