import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("WIJNKOOP_DATA", "/data"))
DB_PATH = DATA_DIR / "wijnkoop.db"
SAMPLES_DIR = DATA_DIR / "samples"
LOG_DIR = DATA_DIR / "logs"

# Honest, generic identification. No personal details.
USER_AGENT = os.environ.get(
    "WIJNKOOP_USER_AGENT",
    "WijnKoop/0.1 (private, non-commercial wine price tracker; slow crawl)",
)
# Minimum seconds between two requests to the same shop. A shop's own
# robots.txt crawl-delay wins if it is longer.
MIN_DELAY = float(os.environ.get("WIJNKOOP_MIN_DELAY", "5"))

# Weekly schedule: day (0=Monday) and hour (local time) of the shop run.
RUN_WEEKDAY = int(os.environ.get("WIJNKOOP_RUN_WEEKDAY", "0"))
RUN_HOUR = int(os.environ.get("WIJNKOOP_RUN_HOUR", "1"))
