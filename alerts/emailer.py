"""
Gmail SMTP alert emailer.

Sends instant alerts when new listings are found, and bulk investor emails
when a deal is marked ready to send.

Uses smtplib + App Passwords — no paid services required.
Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file.
"""

import logging
import os
import smtplib
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _get_credentials() -> tuple[str, str]:
    addr = os.environ.get("GMAIL_ADDRESS", "")
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pwd:
        raise EnvironmentError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in your .env file."
        )
    return addr, pwd


def _send(to: str | list[str], subject: str, html_body: str,
          text_body: str, attachments: list[Path] | None = None) -> bool:
    """Low-level SMTP send. Returns True on success."""
    sender, password = _get_credentials()
    recipients = [to] if isinstance(to, str) else to

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    if attachments:
        outer = MIMEMultipart("mixed")
        outer.attach(msg)
        for path in attachments:
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"'
            )
            outer.attach(part)
        msg = outer

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        logger.info("[Email] Sent '%s' → %s", subject, recipients)
        return True
    except smtplib.SMTPException as exc:
        logger.error("[Email] SMTP error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Property alert emails
# ---------------------------------------------------------------------------

def _listing_html(listing: dict) -> str:
    price = f"£{listing['price']:,}" if listing.get("price") else "POA"
    phone = listing.get("phone") or "Not visible"
    desc = listing.get("description", "")
    snippet = textwrap.shorten(desc, width=300, placeholder="...") if desc else "—"

    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
  <div style="background:#1a3a5c;padding:20px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">🏠 New Property Alert</h2>
  </div>
  <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 8px 8px">
    <h3 style="color:#1a3a5c">{listing.get('title','Untitled')}</h3>
    <table style="width:100%;border-collapse:collapse">
      <tr><td style="padding:6px;font-weight:bold;width:140px">Price</td>
          <td style="padding:6px;color:#27ae60;font-size:1.2em"><strong>{price}</strong></td></tr>
      <tr style="background:#f9f9f9">
          <td style="padding:6px;font-weight:bold">Location</td>
          <td style="padding:6px">{listing.get('location','—')}</td></tr>
      <tr><td style="padding:6px;font-weight:bold">Phone</td>
          <td style="padding:6px;color:#e74c3c"><strong>{phone}</strong></td></tr>
      <tr style="background:#f9f9f9">
          <td style="padding:6px;font-weight:bold">Source</td>
          <td style="padding:6px">{listing.get('source','').capitalize()}</td></tr>
      <tr><td style="padding:6px;font-weight:bold">Posted</td>
          <td style="padding:6px">{listing.get('posted_date','—')}</td></tr>
    </table>
    <h4 style="color:#555;margin-top:16px">Description</h4>
    <p style="color:#333;line-height:1.5">{snippet}</p>
    <a href="{listing.get('url','#')}"
       style="display:inline-block;background:#1a3a5c;color:#fff;padding:12px 24px;
              border-radius:4px;text-decoration:none;margin-top:12px;font-weight:bold">
      View Listing →
    </a>
    <p style="color:#999;font-size:0.8em;margin-top:20px">
      Sent by Property Pipeline — automated sourcing alert
    </p>
  </div>
</body></html>"""


def _listing_text(listing: dict) -> str:
    price = f"£{listing['price']:,}" if listing.get("price") else "POA"
    return (
        f"NEW PROPERTY ALERT\n"
        f"{'='*40}\n"
        f"Title:    {listing.get('title','—')}\n"
        f"Price:    {price}\n"
        f"Location: {listing.get('location','—')}\n"
        f"Phone:    {listing.get('phone') or 'Not visible'}\n"
        f"Source:   {listing.get('source','').capitalize()}\n"
        f"Posted:   {listing.get('posted_date','—')}\n\n"
        f"Description:\n{listing.get('description','—')[:400]}\n\n"
        f"Link: {listing.get('url','—')}\n"
    )


def send_listing_alert(listing: dict, alert_email: str) -> bool:
    """Send a single new-listing alert email."""
    price = f"£{listing['price']:,}" if listing.get("price") else "POA"
    subject = f"🏠 New Property: {listing.get('title','—')} — {price} | {listing.get('location','—')}"
    return _send(
        to=alert_email,
        subject=subject,
        html_body=_listing_html(listing),
        text_body=_listing_text(listing),
    )


def send_batch_alerts(listings: list[dict], alert_email: str) -> int:
    """Send alerts for multiple new listings. Returns number sent successfully."""
    sent = 0
    for listing in listings:
        if send_listing_alert(listing, alert_email):
            sent += 1
    return sent


def _opportunity_tags(listing: dict, keywords: list[str]) -> list[str]:
    """Return which opportunity keywords matched this listing."""
    haystack = " ".join([
        listing.get("title", ""),
        listing.get("description", ""),
        listing.get("location", ""),
    ]).lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def _listing_row_html(listing: dict, tags: list[str]) -> str:
    price = f"£{listing['price']:,}" if listing.get("price") else "POA"
    phone = listing.get("phone") or "—"
    title = listing.get("title", "—")[:70]
    source = listing.get("source", "").capitalize()
    url = listing.get("url", "#")
    tag_html = ""
    if tags:
        tag_html = " ".join(
            f'<span style="display:inline-block;background:#fff3cd;color:#856404;'
            f'border-radius:3px;padding:1px 5px;font-size:0.7em;margin:1px">{t}</span>'
            for t in tags[:4]
        )
    return f"""
    <tr>
      <td style="padding:8px 10px;font-size:0.8em;color:#666">{source}</td>
      <td style="padding:8px 10px">
        <a href="{url}" style="color:#1a3a5c;font-weight:600;text-decoration:none">{title}</a>
        {'<br>' + tag_html if tag_html else ''}
      </td>
      <td style="padding:8px 10px;font-weight:700;color:#1a3a5c;white-space:nowrap">{price}</td>
      <td style="padding:8px 10px;font-size:0.85em">{listing.get('location','—')[:40]}</td>
      <td style="padding:8px 10px;color:#e74c3c;font-weight:700">{phone}</td>
    </tr>"""


def _section_table(listings_with_tags: list[tuple]) -> str:
    if not listings_with_tags:
        return '<p style="color:#7f8c8d;font-style:italic">None found.</p>'
    rows = "".join(_listing_row_html(l, tags) for l, tags in listings_with_tags)
    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:0.875em">
      <thead>
        <tr style="background:#1a3a5c;color:#fff">
          <th style="padding:8px 10px;text-align:left">Source</th>
          <th style="padding:8px 10px;text-align:left">Property</th>
          <th style="padding:8px 10px;text-align:left">Price</th>
          <th style="padding:8px 10px;text-align:left">Location</th>
          <th style="padding:8px 10px;text-align:left">Phone</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def send_daily_summary(
    new_listings: list[dict],
    errors: list[str],
    alert_email: str,
    opportunity_keywords: list[str] | None = None,
) -> bool:
    """
    Send a single daily digest email with all new listings.
    Opportunity properties (matching keywords) are shown first, highlighted.
    Always sends even when 0 listings found.
    """
    from datetime import datetime

    kws = opportunity_keywords or []
    timestamp = datetime.now().strftime("%d %B %Y, %H:%M UTC")

    # Tag each listing
    opportunity = []   # (listing, matched_tags)
    standard = []

    for l in new_listings:
        tags = _opportunity_tags(l, kws)
        if tags:
            opportunity.append((l, tags))
        else:
            standard.append((l, []))

    total = len(new_listings)
    opp_count = len(opportunity)
    otm_count = sum(1 for l, _ in (opportunity + standard) if l.get("source") == "onthemarket")
    rm_count = sum(1 for l, _ in (opportunity + standard) if l.get("source") == "rightmove")
    gt_count = sum(1 for l, _ in (opportunity + standard) if l.get("source") == "gumtree")

    status_colour = "#27ae60" if total > 0 else "#7f8c8d"
    status_text = f"{total} new listing{'s' if total != 1 else ''} found" if total else "No new listings today"

    errors_html = ""
    if errors:
        errors_html = (
            '<div style="background:#fdecea;border-left:4px solid #e74c3c;'
            'padding:10px 14px;margin-top:16px;border-radius:4px">'
            '<strong>Scraper errors:</strong><ul style="margin:4px 0 0">'
            + "".join(f"<li style='font-size:0.85em'>{e}</li>" for e in errors)
            + "</ul></div>"
        )

    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;color:#2c3e50">
  <div style="background:#1a3a5c;padding:22px 24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:1.3em">Property Pipeline — Daily Digest</h2>
    <p style="color:#aac4e4;margin:5px 0 0;font-size:0.9em">{timestamp}</p>
  </div>
  <div style="border:1px solid #ddd;border-top:none;padding:20px 24px;border-radius:0 0 8px 8px">

    <!-- Stats bar -->
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">
      <div style="background:#f4f8ff;border-left:4px solid {status_colour};
                  padding:12px 16px;border-radius:4px;flex:1;min-width:140px">
        <div style="font-size:1.6em;font-weight:700;color:{status_colour}">{total}</div>
        <div style="font-size:0.8em;color:#666">New listings</div>
      </div>
      <div style="background:#fff8e1;border-left:4px solid #f39c12;
                  padding:12px 16px;border-radius:4px;flex:1;min-width:140px">
        <div style="font-size:1.6em;font-weight:700;color:#f39c12">{opp_count}</div>
        <div style="font-size:0.8em;color:#666">Opportunity properties</div>
      </div>
      <div style="background:#f9f9f9;border-left:4px solid #95a5a6;
                  padding:12px 16px;border-radius:4px;flex:1;min-width:140px">
        <div style="font-size:0.85em;color:#555;line-height:1.8">
          OTM: <strong>{otm_count}</strong><br>
          Rightmove: <strong>{rm_count}</strong><br>
          Gumtree: <strong>{gt_count}</strong>
        </div>
      </div>
    </div>

    <!-- Opportunity properties -->
    <h3 style="color:#856404;background:#fff8e1;padding:10px 14px;border-radius:6px;
               border-left:4px solid #f39c12;margin:0 0 12px">
      ⭐ Opportunity Properties ({opp_count})
      <span style="font-weight:400;font-size:0.8em;color:#666">
        — refurb needed / no chain / motivated seller / undervalued
      </span>
    </h3>
    {_section_table(opportunity) if opportunity else
     '<p style="color:#7f8c8d;font-style:italic;margin-bottom:20px">None found today.</p>'}

    <!-- All other new listings -->
    <h3 style="color:#1a3a5c;margin:24px 0 12px">
      All New Listings ({total})
    </h3>
    {_section_table(opportunity + standard)}

    {errors_html}

    <p style="color:#aaa;font-size:0.78em;margin-top:24px;border-top:1px solid #eee;padding-top:12px">
      Property Pipeline — automated daily digest. Call the seller within minutes of a new listing going live.
    </p>
  </div>
</body></html>"""

    # Plain text fallback
    text = f"Property Pipeline Daily Digest — {timestamp}\n{'='*50}\n"
    text += f"Total: {total} | Opportunities: {opp_count} | OTM: {otm_count} | Rightmove: {rm_count}\n\n"
    if opportunity:
        text += "⭐ OPPORTUNITY PROPERTIES\n" + "-"*40 + "\n"
        for l, tags in opportunity:
            price = f"£{l['price']:,}" if l.get("price") else "POA"
            text += f"{l.get('title','?')[:60]} | {price} | {', '.join(tags[:3])}\n{l.get('url','')}\n\n"
    if standard:
        text += "ALL LISTINGS\n" + "-"*40 + "\n"
        for l, _ in standard:
            price = f"£{l['price']:,}" if l.get("price") else "POA"
            text += f"{l.get('title','?')[:60]} | {price} | {l.get('location','')}\n{l.get('url','')}\n\n"
    if errors:
        text += "ERRORS\n" + "\n".join(errors)

    subject = (
        f"🏠 {opp_count} opportunity propert{'ies' if opp_count != 1 else 'y'} found"
        if opp_count > 0
        else f"Property Pipeline: {status_text}"
    )
    subject += f" — {timestamp[:11]}"

    return _send(to=alert_email, subject=subject, html_body=html, text_body=text)


# ---------------------------------------------------------------------------
# Investor deal blast emails
# ---------------------------------------------------------------------------

def _deal_html(deal: dict, investor_name: str, sourcing_fee: int) -> str:
    bmv = f"{deal.get('bmv_percent', 0):.1f}%"
    gross_yield = f"{deal.get('gross_yield', 0):.1f}%"
    net_yield = f"{deal.get('net_yield', 0):.1f}%"
    purchase_price = f"£{deal.get('purchase_price', 0):,}"
    market_value = f"£{deal.get('market_value', 0):,}"
    monthly_rent = f"£{deal.get('monthly_rent', 0):,}" if deal.get("monthly_rent") else "—"
    fee = f"£{sourcing_fee:,}"
    pass_fail_colour = "#27ae60" if deal.get("pass_fail") == "PASS" else "#e74c3c"

    return f"""
<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto">
  <div style="background:#1a3a5c;padding:24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">Investment Opportunity</h2>
    <p style="color:#aac4e4;margin:4px 0 0">Exclusive — Off-Market Property Deal</p>
  </div>
  <div style="border:1px solid #ddd;border-top:none;padding:24px;border-radius:0 0 8px 8px">
    <p>Hi {investor_name},</p>
    <p>I've sourced an off-market deal that matches your investment criteria.
       Full details are below — please review and reply to express interest.</p>

    <h3 style="color:#1a3a5c;border-bottom:2px solid #1a3a5c;padding-bottom:6px">
      Deal Summary
    </h3>
    <table style="width:100%;border-collapse:collapse;font-size:0.95em">
      <tr><td style="padding:8px;font-weight:bold;width:180px">Property</td>
          <td style="padding:8px">{deal.get('address','—')}</td></tr>
      <tr style="background:#f4f8ff">
          <td style="padding:8px;font-weight:bold">Purchase Price</td>
          <td style="padding:8px;font-size:1.1em"><strong>{purchase_price}</strong></td></tr>
      <tr><td style="padding:8px;font-weight:bold">Market Value (GDV)</td>
          <td style="padding:8px">{market_value}</td></tr>
      <tr style="background:#f4f8ff">
          <td style="padding:8px;font-weight:bold">Below Market Value</td>
          <td style="padding:8px;color:{pass_fail_colour};font-weight:bold">{bmv}</td></tr>
      <tr><td style="padding:8px;font-weight:bold">Est. Monthly Rent</td>
          <td style="padding:8px">{monthly_rent}</td></tr>
      <tr style="background:#f4f8ff">
          <td style="padding:8px;font-weight:bold">Gross Rental Yield</td>
          <td style="padding:8px">{gross_yield}</td></tr>
      <tr><td style="padding:8px;font-weight:bold">Net Rental Yield</td>
          <td style="padding:8px">{net_yield}</td></tr>
    </table>

    <div style="background:#fff8e1;border-left:4px solid #f39c12;padding:12px;margin:20px 0">
      <strong>Sourcing Fee: {fee}</strong> — payable on exchange of contracts.
    </div>

    <p>{deal.get('notes','')}</p>

    <p><strong>To express interest, simply reply to this email.</strong>
       A full deal pack (comparables, photos, area report) will be sent to the
       first investor who confirms interest.  A Confidentiality Agreement and
       Sourcing Agreement will also be required before detailed information is
       shared.</p>

    <p style="color:#888;font-size:0.85em;margin-top:24px">
      This deal is shared exclusively with a small group of investors.
      All sourcing activities comply with the Property Ombudsman Code of Practice.
    </p>
  </div>
</body></html>"""


def _deal_text(deal: dict, investor_name: str, sourcing_fee: int) -> str:
    return (
        f"Hi {investor_name},\n\n"
        f"I've sourced an off-market deal that matches your investment criteria.\n\n"
        f"DEAL SUMMARY\n{'='*40}\n"
        f"Property:          {deal.get('address','—')}\n"
        f"Purchase Price:    £{deal.get('purchase_price',0):,}\n"
        f"Market Value:      £{deal.get('market_value',0):,}\n"
        f"Below Market Value: {deal.get('bmv_percent',0):.1f}%\n"
        f"Est. Monthly Rent: £{deal.get('monthly_rent',0):,}\n"
        f"Gross Yield:       {deal.get('gross_yield',0):.1f}%\n"
        f"Net Yield:         {deal.get('net_yield',0):.1f}%\n\n"
        f"SOURCING FEE: £{sourcing_fee:,}\n\n"
        f"{deal.get('notes','')}\n\n"
        f"Reply to this email to express interest.\n"
    )


def send_deal_to_investor(deal: dict, investor: dict, sourcing_fee: int,
                          attachments: list[Path] | None = None) -> bool:
    """Send a deal summary email to one investor."""
    name = investor.get("name", "Investor")
    subject = (
        f"Exclusive Deal: {deal.get('address','Property')} — "
        f"{deal.get('bmv_percent',0):.0f}% BMV | "
        f"{deal.get('gross_yield',0):.1f}% Yield"
    )
    return _send(
        to=investor["email"],
        subject=subject,
        html_body=_deal_html(deal, name, sourcing_fee),
        text_body=_deal_text(deal, name, sourcing_fee),
        attachments=attachments,
    )
