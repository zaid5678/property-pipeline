"""
Deal pack PDF generator using ReportLab (free, no external dependencies).
Produces a professional one-page deal summary ready to send to investors.
"""

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "output"


def generate_deal_pdf(deal: dict, sourcing_fee: int = 3000) -> Path:
    """
    Generate a PDF deal pack for the given deal dict.
    Returns the Path to the generated file.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
    except ImportError:
        logger.error("reportlab not installed. Run: pip install reportlab")
        raise

    OUTPUT_DIR.mkdir(exist_ok=True)

    ref = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = OUTPUT_DIR / f"deal_pack_{ref}.pdf"

    doc = SimpleDocTemplate(
        str(filename),
        pagesize=A4,
        rightMargin=15*mm,
        leftMargin=15*mm,
        topMargin=15*mm,
        bottomMargin=15*mm,
    )

    NAVY = colors.HexColor("#1a3a5c")
    GREEN = colors.HexColor("#27ae60")
    RED = colors.HexColor("#e74c3c")
    AMBER = colors.HexColor("#f39c12")
    LIGHT_BLUE = colors.HexColor("#f4f8ff")
    GRAY = colors.HexColor("#555555")

    styles = getSampleStyleSheet()
    heading1 = ParagraphStyle(
        "Heading1Custom", parent=styles["Heading1"],
        textColor=NAVY, fontSize=18, spaceAfter=4,
    )
    heading2 = ParagraphStyle(
        "Heading2Custom", parent=styles["Heading2"],
        textColor=NAVY, fontSize=13, spaceAfter=2,
    )
    normal = ParagraphStyle(
        "NormalCustom", parent=styles["Normal"],
        fontSize=10, leading=14,
    )
    small = ParagraphStyle(
        "Small", parent=styles["Normal"],
        fontSize=8, textColor=GRAY,
    )

    pf = deal.get("pass_fail", "?")
    pf_colour = GREEN if pf == "PASS" else (AMBER if pf == "INCOMPLETE" else RED)

    def _fmt_currency(val) -> str:
        return f"£{int(val):,}" if val else "—"

    def _fmt_pct(val) -> str:
        return f"{float(val):.1f}%" if val is not None else "—"

    story = []

    # Header
    story.append(Paragraph("INVESTMENT DEAL PACK", heading1))
    story.append(Paragraph("Confidential — Exclusive Off-Market Opportunity", small))
    story.append(HRFlowable(width="100%", thickness=2, color=NAVY, spaceAfter=8))

    # Property & Reference
    story.append(Paragraph(f"<b>Property:</b> {deal.get('address','—')}", normal))
    story.append(Paragraph(
        f"<b>Reference:</b> DP-{ref[:8]} &nbsp;&nbsp; "
        f"<b>Date:</b> {datetime.now().strftime('%d %B %Y')}",
        normal
    ))
    story.append(Spacer(1, 8))

    # Key figures table
    story.append(Paragraph("Key Figures", heading2))

    pass_text = f"<font color='{'#27ae60' if pf=='PASS' else '#e74c3c'}'><b>{pf}</b></font>"
    figures = [
        ["Purchase Price", _fmt_currency(deal.get("purchase_price")),
         "Verdict", pass_text],
        ["Market Value (GDV)", _fmt_currency(deal.get("market_value")),
         "Comparables Used", str(deal.get("comparables_count", "—"))],
        ["Below Market Value", _fmt_pct(deal.get("bmv_percent")),
         "Rental Samples", str(deal.get("rental_sample_size", "—"))],
        ["Est. Monthly Rent", _fmt_currency(deal.get("monthly_rent")),
         "Annual Rent", _fmt_currency(deal.get("annual_rent"))],
        ["Gross Rental Yield", _fmt_pct(deal.get("gross_yield")),
         "Net Rental Yield", _fmt_pct(deal.get("net_yield"))],
        ["Sourcing Fee", _fmt_currency(sourcing_fee),
         "Assumed Costs", _fmt_pct(deal.get("costs_percent"))],
    ]

    tbl = Table(figures, colWidths=[45*mm, 35*mm, 45*mm, 35*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
        ("BACKGROUND", (0, 2), (-1, 2), LIGHT_BLUE),
        ("BACKGROUND", (0, 4), (-1, 4), LIGHT_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUND", (0, 0), (-1, -1), [colors.white, LIGHT_BLUE]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 10))

    # Comparables
    comps = deal.get("comparables", [])
    if comps:
        story.append(Paragraph("Recent Sold Comparables (Land Registry)", heading2))
        comp_data = [["Address", "Sold Price", "Date", "Type"]]
        for c in comps[:8]:
            comp_data.append([
                c.get("address", "—")[:50],
                _fmt_currency(c.get("price")),
                c.get("date", "—"),
                c.get("type", "—"),
            ])
        comp_tbl = Table(comp_data, colWidths=[70*mm, 30*mm, 30*mm, 25*mm])
        comp_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUND", (0, 1), (-1, -1), [colors.white, LIGHT_BLUE]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(comp_tbl)
        story.append(Spacer(1, 10))

    # Notes
    if deal.get("notes"):
        story.append(Paragraph("Notes", heading2))
        story.append(Paragraph(deal["notes"], normal))
        story.append(Spacer(1, 8))

    # Footer
    story.append(HRFlowable(width="100%", thickness=1, color=GRAY, spaceAfter=4))
    story.append(Paragraph(
        f"Sourcing Fee: <b>{_fmt_currency(sourcing_fee)}</b> — payable on exchange of contracts. "
        "A Confidentiality Agreement and Sourcing Agreement must be signed before "
        "detailed property information is released. This document is strictly confidential.",
        small
    ))
    story.append(Paragraph(
        "Source data: UK Land Registry Price Paid Data (Crown copyright), "
        "Rightmove rental listings. Market value is an estimate only — conduct your own due diligence.",
        small
    ))

    doc.build(story)
    logger.info("[PDF] Deal pack saved: %s", filename)
    print(f"[PDF] Deal pack generated: {filename}")
    return filename
