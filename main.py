"""
Property Pipeline — main entry point.

Runs the scraper on a schedule and sends instant alerts for new listings.

Usage:
  python main.py             # Start the scheduler (runs indefinitely)
  python main.py --once      # Run scrapers once and exit (good for testing)
  python main.py --dashboard # Launch the web dashboard instead
"""

import argparse
import logging
import os
import sys
from datetime import datetime

import schedule
import time
import yaml
from dotenv import load_dotenv

load_dotenv()

# Configure logging before importing anything else
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

from db.models import init_db
from scraper.gumtree import run_scraper as gumtree_scraper
from scraper.rightmove import run_scraper as rightmove_scraper
from alerts.emailer import send_batch_alerts


def load_config() -> dict:
    path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def run_scrape_cycle(config: dict) -> None:
    """Run all scrapers, deduplicate, and send alerts for new listings."""
    logger.info("=" * 60)
    logger.info("Starting scrape cycle — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    all_new = []

    # Gumtree
    try:
        new_gumtree = gumtree_scraper(config)
        all_new.extend(new_gumtree)
        logger.info("[Main] Gumtree: %d new listings", len(new_gumtree))
    except Exception as exc:
        logger.error("[Main] Gumtree scraper failed: %s", exc)

    # Rightmove reduced
    try:
        new_rightmove = rightmove_scraper(config)
        all_new.extend(new_rightmove)
        logger.info("[Main] Rightmove: %d new listings", len(new_rightmove))
    except Exception as exc:
        logger.error("[Main] Rightmove scraper failed: %s", exc)

    logger.info("[Main] Total new listings this cycle: %d", len(all_new))

    # Send alerts
    if all_new and config.get("alerts", {}).get("email_enabled", True):
        alert_email = os.environ.get("ALERT_EMAIL", "")
        if not alert_email:
            logger.warning("[Main] ALERT_EMAIL not set — skipping email alerts")
        else:
            sent = send_batch_alerts(all_new, alert_email)
            logger.info("[Main] Sent %d alert emails to %s", sent, alert_email)

    logger.info("[Main] Scrape cycle complete.\n")


def start_scheduler(config: dict) -> None:
    interval = config["scraper"].get("interval_minutes", 60)
    logger.info("Scheduler starting — will scrape every %d minutes", interval)
    logger.info("Target areas: %s", ", ".join(config["scraper"]["target_areas"]))
    logger.info(
        "Price range: £%s – £%s",
        f"{config['scraper']['price']['min']:,}",
        f"{config['scraper']['price']['max']:,}",
    )

    # Run once immediately on startup
    run_scrape_cycle(config)

    # Schedule recurring runs
    schedule.every(interval).minutes.do(run_scrape_cycle, config=config)

    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="Property Pipeline")
    parser.add_argument("--once", action="store_true",
                        help="Run scrapers once and exit")
    parser.add_argument("--dashboard", action="store_true",
                        help="Launch the web dashboard")
    args = parser.parse_args()

    # Initialise database
    init_db()
    config = load_config()

    if args.dashboard:
        logger.info("Launching dashboard at http://localhost:5000")
        from dashboard.app import app
        app.run(debug=False, port=5000)
        return

    if args.once:
        run_scrape_cycle(config)
        return

    start_scheduler(config)


if __name__ == "__main__":
    main()
