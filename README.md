# Property Pipeline 🏠

A complete UK property sourcing automation system for solo operators. Scrapes Gumtree and Rightmove for motivated sellers, sends instant alerts, analyses deals with Land Registry data, manages investors in a CRM, and auto-generates legal documents — all **completely free**, no paid APIs or credit card required.

**Target income: £2,000–£5,000 per deal (sourcing fee).**

---

## What it does

| Module | What it does |
|---|---|
| **Scraper** | Scrapes Gumtree & Rightmove every 60 minutes for properties matching your price range and target areas |
| **Alerts** | Emails you instantly when a new listing is found — beat other buyers to the phone |
| **Deal Analyser** | Pulls Land Registry sold data + Rightmove rental estimates to calculate BMV% and yield |
| **PDF Generator** | Creates a professional deal pack PDF ready to send to investors |
| **Investor CRM** | SQLite database tracking investors, deals sent, responses, and follow-ups |
| **Email Blast** | Sends deal summaries to matching investors via Gmail |
| **Document Automation** | Auto-generates NDA and Sourcing Agreement as .docx files |
| **Dashboard** | Flask web UI to view properties, manage the deal pipeline, and track investors |

---

## Prerequisites

- Python 3.10 or newer
- A Gmail account (for sending alerts and investor emails)

---

## Setup (step by step)

### 1. Clone the repository

```bash
git clone https://github.com/zaid5678/property-pipeline.git
cd property-pipeline
```

### 2. Create a virtual environment

```bash
# Mac/Linux
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### 3. Install all dependencies

```bash
pip install -r requirements.txt
```

All packages are free and open source. Nothing requires a credit card.

### 4. Set up Gmail App Password

You need a **Gmail App Password** (NOT your normal password):

1. Go to your Google Account → **Security** → **2-Step Verification** (enable it if not already)
2. Then go to **Security** → **App passwords**
3. Create a new app password — name it "Property Pipeline"
4. Copy the 16-character password shown

### 5. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your details:

```env
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
ALERT_EMAIL=you@gmail.com
```

### 6. Configure your search criteria

Edit `config.yaml`:

```yaml
scraper:
  target_areas:
    - Birmingham
    - Manchester
    - Leeds
    # Add/remove any UK towns or cities

  price:
    min: 50000
    max: 250000

analysis:
  min_bmv_percent: 15    # Minimum BMV% to PASS
  min_gross_yield: 7.0   # Minimum gross yield % to PASS

deals:
  sourcing_fee: 3000     # Your fee in £
```

### 7. Initialise the database and run once (test)

```bash
python main.py --once
```

This runs both scrapers once and sends alert emails for any new listings found. Check your inbox.

### 8. Start the scheduler (leave running)

```bash
python main.py
```

The scraper will run every 60 minutes automatically. Keep this terminal open, or run it in the background.

**To run in background on Mac/Linux:**
```bash
nohup python main.py &
```

**To run in background on Windows:** Use Task Scheduler or simply leave the terminal open.

---

## Using the Dashboard

```bash
python main.py --dashboard
```

Then open **http://localhost:5000** in your browser.

The dashboard shows:
- All scraped properties (filterable by source, price, area)
- Deal pipeline (drag-and-drop style status tracker)
- Investor list with follow-up flags
- Total fees earned

---

## Using the CRM (command line)

### Add an investor

```bash
python -m crm.cli investors add
```

Follow the prompts to enter name, email, strategy (BTL/HMO/SA), preferred areas, and budget.

### List all investors

```bash
python -m crm.cli investors list
```

### Analyse a deal

```bash
python -m crm.cli deals analyse
```

Enter the property address, postcode, purchase price, and bedrooms. The system will:
1. Pull Land Registry sold comparables for the postcode
2. Scrape Rightmove rental listings for a rent estimate
3. Calculate BMV%, gross yield, net yield
4. Show PASS or FAIL with reasons
5. Optionally save to the database and generate a PDF deal pack

### Send a deal to investors

```bash
python -m crm.cli deals send <deal_id>
```

Automatically emails all investors whose criteria match the deal. Attaches the PDF deal pack.

### List deals

```bash
python -m crm.cli deals list
python -m crm.cli deals list --status analysed
```

### Update deal status

```bash
python -m crm.cli deals status 1 under_offer
```

Statuses: `found → analysed → sent → under_offer → completed → dead`

### Check follow-ups needed

```bash
python -m crm.cli follow-up
```

Shows any investors who received a deal email but haven't responded in 48 hours.

### Record a fee received

```bash
python -m crm.cli fee
```

### Record an investor's response

```bash
python -m crm.cli deals respond <deal_id> <investor_id> interested
```

---

## Generating Legal Documents

Documents are generated automatically when you run `deals analyse` or `deals send`, but you can also generate them manually:

```python
from documents.generator import generate_nda, generate_sourcing_agreement

generate_nda(
    investor_name="John Smith",
    property_ref="DP-20240427-001",
    sourcer_name="Your Name",
    sourcer_company="Your Company",
)

generate_sourcing_agreement(
    investor_name="John Smith",
    investor_email="john@example.com",
    property_address="123 High Street, Birmingham, B1 1AA",
    property_ref="DP-20240427-001",
    purchase_price=150000,
    sourcing_fee=3000,
    sourcer_name="Your Name",
    sourcer_company="Your Company",
)
```

Documents are saved to the `output/` folder as `.docx` files ready to send.

---

## Project structure

```
property-pipeline/
├── main.py               ← Entry point: scheduler + dashboard launcher
├── config.yaml           ← Your search settings
├── .env                  ← Gmail credentials (never commit this)
├── requirements.txt
│
├── scraper/
│   ├── gumtree.py        ← Gumtree property scraper
│   ├── rightmove.py      ← Rightmove reduced listings scraper
│   └── base.py           ← Shared HTTP utilities
│
├── alerts/
│   └── emailer.py        ← Gmail SMTP alerts + investor blast emails
│
├── analyser/
│   ├── land_registry.py  ← Land Registry Price Paid API
│   ├── rental_estimator.py ← Rightmove rental comparable scraper
│   ├── deal_calculator.py  ← BMV + yield calculator
│   └── pdf_generator.py    ← ReportLab PDF deal pack generator
│
├── crm/
│   ├── database.py       ← CRM database layer (CRUD)
│   └── cli.py            ← Command-line interface
│
├── documents/
│   └── generator.py      ← NDA + Sourcing Agreement (.docx)
│
├── dashboard/
│   ├── app.py            ← Flask web app
│   └── templates/        ← HTML templates
│
└── db/
    └── models.py         ← SQLite schema + connection helper
```

Data is stored in `data/pipeline.db` (SQLite — no setup needed).
Logs are written to `pipeline.log`.
Generated files (PDFs, .docx) go to `output/`.

---

## All pip packages used

```
requests          — HTTP client for scraping
beautifulsoup4    — HTML parser
lxml              — Fast XML/HTML parser (used by BeautifulSoup)
schedule          — Task scheduler
python-dotenv     — Loads .env file
PyYAML            — Reads config.yaml
reportlab         — PDF generation (deal packs)
python-docx       — Word document generation (NDA, agreements)
Flask             — Web dashboard
click             — CLI framework
tabulate          — Pretty-print tables in the terminal
```

---

## Important notes

**Scraping ethics:** This tool respects servers by adding delays between requests (configurable in `config.yaml` via `request_delay`). Do not set `request_delay` below 2 seconds. Gumtree and Rightmove may update their HTML structure — if the scraper stops finding listings, the selectors in `scraper/gumtree.py` and `scraper/rightmove.py` will need updating.

**Legal compliance:** Property sourcing in the UK is regulated under the Estate Agents Act 1979. Ensure you are operating in compliance with the Property Ombudsman Code of Practice. The documents generated by this system are templates — have them reviewed by a solicitor before use.

**Data:** Land Registry Price Paid Data is Crown copyright and used under the Open Government Licence v3.0.

---

## Troubleshooting

**Scraper finds 0 listings:**
- Gumtree may have changed their HTML — check selectors in `scraper/gumtree.py`
- Try running with `--once` and check `pipeline.log` for errors

**Email not sending:**
- Confirm `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in `.env` are correct
- Make sure you're using an **App Password**, not your regular Gmail password
- Check that 2-Step Verification is enabled on your Google account

**Land Registry returning no data:**
- The API can be slow — try a specific full postcode (e.g. `B1 1AA` not just `B1`)
- Check `pipeline.log` for the SPARQL/REST response

**Dashboard not loading:**
- Make sure you've run `python main.py --once` first to create the database
- Check port 5000 isn't in use by another application
